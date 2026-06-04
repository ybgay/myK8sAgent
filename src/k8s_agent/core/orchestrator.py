"""Master Agent Orchestrator — coordinates multi-agent task execution.

The orchestrator uses Claude to:
1. Understand user intent
2. Decompose complex tasks into sub-tasks (Task DAG)
3. Delegate to specialized agents via the MessageBus
4. Synthesize results into a coherent response
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from k8s_agent.core.communication.message_bus import MessageBus
from k8s_agent.core.communication.context import SharedContext
from k8s_agent.core.agents.base import BaseAgent
from k8s_agent.core.mcp.client_manager import MCPClientManager
from k8s_agent.llm.provider import CompletionRequest, LLMProvider
from k8s_agent.llm.conversation import ConversationManager
from k8s_agent.llm.prompts import MASTER_AGENT_PROMPT, render_prompt
from k8s_agent.shared.config import get_settings
from k8s_agent.shared.logging import get_logger
from k8s_agent.shared.types import (
    AgentIdentity,
    ContentBlock,
    Message,
    TaskRequest,
    TaskResult,
    TaskStatus,
    ToolDefinition,
    ToolResult,
)

logger = get_logger(__name__)


# ============================================================================
# Task Decomposer
# ============================================================================

@dataclass
class TaskNode:
    """A node in the task DAG."""
    task_id: str
    description: str
    assignee: str  # "k8s" | skill name | "master"
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)  # task_ids this depends on
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None


class TaskDecomposer:
    """Decomposes high-level user requests into a DAG of sub-tasks.

    Uses the LLM to understand the request and break it down into
    executable operations with dependencies.
    """

    def __init__(self, llm_provider: LLMProvider) -> None:
        self._llm = llm_provider

    async def decompose(
        self,
        request: str,
        available_agents: list[dict[str, Any]],
        available_tools: list[dict[str, Any]],
    ) -> list[TaskNode]:
        """Decompose a user request into a task DAG.

        Args:
            request: The user's natural language request.
            available_agents: List of available agents and their capabilities.
            available_tools: List of available tools.

        Returns:
            List of TaskNode objects forming the execution DAG.
        """
        # Build a planning prompt
        agents_desc = json.dumps(available_agents, indent=2)
        tools_desc = json.dumps([t["name"] for t in available_tools], indent=2)

        planning_prompt = f"""将以下 Kubernetes 请求拆解为任务序列。

可用代理：
{agents_desc}

可用工具：{tools_desc}

用户请求：{request}

输出一个 JSON 任务列表。每个任务包含：
- "description"：任务描述
- "assignee"：执行代理（"k8s" 或 "master"）
- "action"：工具名称或操作
- "params"：操作参数
- "depends_on"：必须在此任务之前完成的任务描述列表（无依赖则为空列表）

保持简洁——只为真正需要顺序的操作创建独立任务。
对于简单的只读请求，一个单独任务即可。

只输出 JSON 列表，不要输出其他内容。"""

        try:
            response = await self._llm.complete(CompletionRequest(
                model=self._llm.config.model,
                system_prompt="你是一个任务规划器。只输出合法的 JSON。请使用简体中文。",
                messages=[
                    Message(
                        role="user",
                        content=[ContentBlock(type="text", text=planning_prompt)],
                    )
                ],
                max_tokens=2048,
                temperature=0.1,
            ))

            # Parse the JSON response
            tasks_json = _extract_json(response.content)
            nodes = []
            for i, t in enumerate(tasks_json):
                node = TaskNode(
                    task_id=f"task-{i:03d}",
                    description=t.get("description", f"Task {i}"),
                    assignee=t.get("assignee", "k8s"),
                    action=t.get("action", ""),
                    params=t.get("params", {}),
                    depends_on=[],
                )
                nodes.append(node)

            # Resolve depends_on by matching descriptions to task_ids
            for node in nodes:
                resolved_deps = []
                for dep_desc in t.get("depends_on", []):
                    for other in nodes:
                        if other.description == dep_desc:
                            resolved_deps.append(other.task_id)
                            break
                node.depends_on = resolved_deps

            logger.info("task_decomposition", request=request[:100], task_count=len(nodes))
            return nodes

        except Exception as e:
            logger.error("task_decomposition_failed", error=str(e))
            # Return a single fallback task
            return [
                TaskNode(
                    task_id="task-000",
                    description=request,
                    assignee="k8s",
                    action="execute",
                    params={"request": request},
                )
            ]


def _extract_json(text: str) -> list[dict[str, Any]]:
    """Extract a JSON list from text that may contain other content."""
    text = text.strip()
    # Try direct parse
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        return [result]
    except json.JSONDecodeError:
        pass

    # Try to find JSON between [ and ]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    # Return as single task
    return [{"description": text, "assignee": "k8s", "action": "execute"}]


# ============================================================================
# Plan Executor
# ============================================================================

class PlanExecutor:
    """Executes a task DAG respecting dependencies.

    Tasks at the same dependency level run concurrently.
    """

    def __init__(
        self,
        message_bus: MessageBus,
        max_parallel: int = 3,
    ) -> None:
        self.message_bus = message_bus
        self.max_parallel = max_parallel

    async def execute(
        self,
        tasks: list[TaskNode],
        timeout_ms: int = 300_000,
    ) -> dict[str, TaskResult]:
        """Execute a list of tasks respecting their dependencies.

        Args:
            tasks: The task DAG nodes.
            timeout_ms: Per-task timeout.

        Returns:
            Dict mapping task_id -> TaskResult.
        """
        results: dict[str, TaskResult] = {}
        pending = {t.task_id: t for t in tasks}
        completed: set[str] = set()
        failed: set[str] = set()
        in_flight: dict[str, asyncio.Task] = {}

        logger.info("plan_executor_start", task_count=len(tasks))

        while pending or in_flight:
            # Find tasks whose dependencies are all satisfied
            ready = [
                t for t in pending.values()
                if all(
                    dep in completed
                    for dep in t.depends_on
                )
                and len(in_flight) < self.max_parallel
            ]

            # Launch ready tasks
            for task in ready:
                if any(dep in failed for dep in task.depends_on):
                    # Skip tasks whose dependencies failed
                    task.status = TaskStatus.CANCELLED
                    results[task.task_id] = TaskResult(
                        task_id=task.task_id,
                        status=TaskStatus.CANCELLED,
                        error="Dependency failed",
                    )
                    failed.add(task.task_id)
                    del pending[task.task_id]
                    continue

                task.status = TaskStatus.RUNNING
                del pending[task.task_id]

                req = TaskRequest(
                    task_id=task.task_id,
                    task_type=task.assignee,
                    action=task.action,
                    params=task.params,
                    timeout_ms=timeout_ms,
                )

                in_flight[task.task_id] = asyncio.create_task(
                    self._dispatch_and_wait(task, req)
                )

            # Wait for at least one task to complete
            if in_flight:
                done, _ = await asyncio.wait(
                    list(in_flight.values()),
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=1.0,
                )

                for done_task in done:
                    # Find which task_id this corresponds to
                    for tid, fut in list(in_flight.items()):
                        if fut is done_task:
                            result = await fut
                            results[tid] = result
                            if result.status == TaskStatus.SUCCESS:
                                completed.add(tid)
                            else:
                                failed.add(tid)
                            del in_flight[tid]
                            break

            # Small sleep to avoid busy looping
            if not ready and not in_flight:
                await asyncio.sleep(0.05)

        logger.info(
            "plan_executor_done",
            completed=len(completed),
            failed=len(failed),
        )
        return results

    async def _dispatch_and_wait(
        self,
        task: TaskNode,
        request: TaskRequest,
    ) -> TaskResult:
        """Dispatch a task to the appropriate agent and wait for result."""
        topic = f"agent.{task.assignee}.{task.action}"

        try:
            result = await self.message_bus.request(
                topic,
                request,
                timeout=request.timeout_ms / 1000 if request.timeout_ms else 30.0,
            )
            task.result = result
            task.status = (
                TaskStatus.SUCCESS
                if (isinstance(result, TaskResult) and result.status == TaskStatus.SUCCESS)
                or result is not None
                else TaskStatus.FAILED
            )
            if isinstance(result, TaskResult):
                return result
            return TaskResult(
                task_id=task.task_id,
                status=TaskStatus.SUCCESS,
                output=result,
            )
        except asyncio.TimeoutError:
            task.status = TaskStatus.FAILED
            return TaskResult(
                task_id=task.task_id,
                status=TaskStatus.FAILED,
                error="Task timed out",
            )
        except Exception as e:
            task.status = TaskStatus.FAILED
            return TaskResult(
                task_id=task.task_id,
                status=TaskStatus.FAILED,
                error=str(e),
            )


# ============================================================================
# Master Agent
# ============================================================================

@dataclass
class MasterAgent:
    """The main orchestrator agent.

    Receives user requests, uses Claude to plan and execute,
    delegates to sub-agents, and synthesizes results.

    Usage:
        master = MasterAgent(llm_provider, message_bus, context)
        response = await master.run("List all failing pods in production")
    """

    llm_provider: LLMProvider
    message_bus: MessageBus
    context: SharedContext
    mcp_client_manager: MCPClientManager | None = None
    available_agents: list[dict[str, Any]] = field(default_factory=list)

    # Internal state
    conversation: ConversationManager = field(default_factory=ConversationManager)
    task_decomposer: TaskDecomposer | None = None
    plan_executor: PlanExecutor | None = None

    def __post_init__(self) -> None:
        settings = get_settings()
        self.task_decomposer = TaskDecomposer(self.llm_provider)
        self.plan_executor = PlanExecutor(
            self.message_bus,
            max_parallel=settings.agent.parallel_task_limit,
        )
        self.max_iterations = settings.agent.max_iterations

    async def initialize(self) -> None:
        """Initialize the master agent."""
        # Gather available tools from all sources
        tools = await self._get_all_tools()
        logger.info("master_agent_initialized", tool_count=len(tools))

    async def run(self, user_request: str) -> str:
        """Process a user request end-to-end.

        Args:
            user_request: Natural language request from the user.

        Returns:
            Final response text.
        """
        start_time = time.monotonic()

        # Set system prompt (refreshed each turn for updated context)
        # NOTE: Do NOT clear conversation — history is managed by the API layer
        # via SessionManager so multi-turn conversations work correctly.
        self.conversation.system_prompt = self._build_system_prompt()

        logger.info("master_agent_run", request=user_request[:100])

        # Phase 1: Decompose complex requests
        if self._should_decompose(user_request):
            tasks = await self.task_decomposer.decompose(
                user_request,
                self.available_agents,
                await self._get_all_tools(),
            )
            if len(tasks) > 1:
                logger.info("decomposed_to_tasks", count=len(tasks))
                results = await self.plan_executor.execute(tasks)
                return await self._synthesize_results(user_request, tasks, results)

        # Phase 2: Single-turn agentic loop
        return await self._agentic_loop(user_request)

    async def run_stream(self, user_request: str):
        """Process a user request and stream responses (generator).

        Yields text deltas and tool_call events for real-time UI updates.
        """
        start_time = time.monotonic()

        # NOTE: Do NOT clear conversation — history is managed by the API layer
        # via SessionManager so multi-turn conversations work correctly.
        self.conversation.system_prompt = self._build_system_prompt()
        self.conversation.add_user_message(user_request)

        tools = await self._get_all_tools()

        for iteration in range(self.max_iterations):
            request = CompletionRequest(
                model=self.llm_provider.config.model,
                system_prompt=self.conversation.system_prompt,
                messages=self.conversation.messages,
                tools=tools,
                max_tokens=self.llm_provider.config.max_tokens,
                temperature=self.llm_provider.config.temperature,
                stream=True,
            )

            tool_calls_buffer: list[dict[str, Any]] = []
            current_text = ""
            current_tool_name = ""
            current_tool_input = ""

            async for event in self.llm_provider.complete_stream(request):
                yield event

                if event.type == "text_delta":
                    current_text += event.text or ""
                elif event.type == "tool_use_start":
                    current_tool_name = event.tool_name or ""
                    current_tool_input = ""
                elif event.type == "tool_use_delta":
                    current_tool_input += event.input_json_delta or ""
                elif event.type == "tool_use_end":
                    tool_calls_buffer.append({
                        "id": event.tool_use_id,
                        "name": event.tool_name,
                        "input": event.tool_input or {},
                    })

            # If no tool calls, we're done
            if not tool_calls_buffer:
                self.conversation.add_assistant_message(current_text)
                break

            # Execute tools
            self.conversation.add_full_assistant_turn(current_text, tool_calls_buffer)
            tool_results = []
            for tc in tool_calls_buffer:
                yield {
                    "type": "tool_executing",
                    "tool_name": tc["name"],
                    "tool_input": tc["input"],
                }
                result = await self._execute_tool(tc["name"], tc["input"])
                tool_results.append((tc["id"], result))

                # Check if result contains topology data for visualization
                viz = _extract_visualization(result, tc["name"])
                if viz:
                    yield {
                        "type": "visualization",
                        "data": viz,
                        "source_tool": tc["name"],
                    }

                yield {
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    "result": result,
                }

            self.conversation.add_tool_results_batch(tool_results)

        elapsed = (time.monotonic() - start_time) * 1000
        logger.info("master_agent_stream_done", elapsed_ms=round(elapsed, 1))

    async def _agentic_loop(self, user_request: str) -> str:
        """Execute the core agentic loop: think → act → observe → repeat."""
        self.conversation.add_user_message(user_request)
        tools = await self._get_all_tools()

        for iteration in range(self.max_iterations):
            request = CompletionRequest(
                model=self.llm_provider.config.model,
                system_prompt=self.conversation.system_prompt,
                messages=self.conversation.messages,
                tools=tools,
                max_tokens=self.llm_provider.config.max_tokens,
                temperature=self.llm_provider.config.temperature,
            )

            response = await self.llm_provider.complete(request)

            if response.tool_calls:
                # Execute tools and add results to conversation
                self.conversation.add_full_assistant_turn(
                    response.content, response.tool_calls
                )
                tool_results = []
                for tc in response.tool_calls:
                    result = await self._execute_tool(tc["name"], tc["input"])
                    tool_results.append((tc["id"], result))

                self.conversation.add_tool_results_batch(tool_results)
            else:
                # No tool calls — task complete
                self.conversation.add_assistant_message(response.content)
                return response.content

        return "Maximum iterations reached. Please try a simpler request."

    async def _execute_tool(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> str:
        """Execute a tool from any source.

        Tool name format:
        - "server_name/tool_name" → MCP tool from that server
        - "tool_name" → Local tool or default MCP server
        - "delegate" → Delegate to sub-agent
        """
        # Handle delegate tool
        if tool_name == "delegate":
            return await self._handle_delegate(tool_input)

        # Handle prefixed MCP tools (format: server__tool_name)
        if "__" in tool_name:
            server, name = tool_name.split("__", 1)
            if self.mcp_client_manager:
                try:
                    return await self.mcp_client_manager.call_tool(
                        server, name, tool_input
                    )
                except Exception as e:
                    return json.dumps({"error": True, "message": str(e)})

        # Default: try built-in K8s MCP server
        if self.mcp_client_manager and self.mcp_client_manager.is_connected("k8s-agent-mcp-server"):
            try:
                return await self.mcp_client_manager.call_tool(
                    "k8s-agent-mcp-server", tool_name, tool_input
                )
            except Exception as e:
                return json.dumps({"error": True, "message": str(e)})

        # Fallback: direct tool call
        return await self._execute_tool_direct(tool_name, tool_input)

    async def _execute_tool_direct(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> str:
        """Execute a tool directly (fallback without MCP)."""
        # Try importing and calling the tool function directly
        mapping = {
            "list_pods": ("k8s_agent.mcp_server.tools.pods", "list_pods"),
            "get_pod": ("k8s_agent.mcp_server.tools.pods", "get_pod"),
            "list_deployments": ("k8s_agent.mcp_server.tools.deployments", "list_deployments"),
            "list_namespaces": ("k8s_agent.mcp_server.tools.namespaces", "list_namespaces"),
            "list_services": ("k8s_agent.mcp_server.tools.services", "list_services"),
            "list_nodes": ("k8s_agent.mcp_server.tools.nodes", "list_nodes"),
            "list_events": ("k8s_agent.mcp_server.tools.events", "list_events"),
            "get_cluster_info": ("k8s_agent.mcp_server.tools.metrics", "get_cluster_info"),
        }

        if tool_name in mapping:
            try:
                module_path, func_name = mapping[tool_name]
                import importlib
                module = importlib.import_module(module_path)
                func = getattr(module, func_name, None)
                if func:
                    return await func(**tool_input)
            except Exception as e:
                return json.dumps({"error": True, "message": str(e)})

        return json.dumps({
            "error": True,
            "message": f"Tool '{tool_name}' not available. Start the MCP server with 'k8s-agent serve'.",
        })

    async def _handle_delegate(self, tool_input: dict[str, Any]) -> str:
        """Handle the delegate tool — route to sub-agent via MessageBus."""
        agent_type = tool_input.get("agent", "k8s")
        task_desc = tool_input.get("task", "")
        context_override = tool_input.get("context", {})

        request = TaskRequest(
            task_type=agent_type,
            action="execute",
            params={
                "task": task_desc,
                "context": context_override,
            },
        )

        topic = f"agent.{agent_type}.execute"
        try:
            result = await self.message_bus.request(topic, request, timeout=60.0)
            return str(result)
        except asyncio.TimeoutError:
            return json.dumps({"error": True, "message": "Sub-agent timed out"})

    async def _synthesize_results(
        self,
        user_request: str,
        tasks: list[TaskNode],
        results: dict[str, TaskResult],
    ) -> str:
        """Synthesize task execution results into a coherent response."""
        summary_parts = []
        for task in tasks:
            result = results.get(task.task_id)
            status = result.status.value if result else "unknown"
            summary_parts.append(
                f"- [{status.upper()}] {task.description}: "
                f"{result.output if result else 'no result'}"
            )

        summary = "\n".join(summary_parts)

        # Use LLM to produce a natural language response
        synthesis_prompt = f"""将以下任务结果综合成一个简洁的回复。

原始请求：{user_request}

任务结果：
{summary}

提供一个清晰、有建设性的总结。包含具体的资源名称和状态。请使用简体中文回复。"""

        try:
            response = await self.llm_provider.complete(CompletionRequest(
                model=self.llm_provider.config.model,
                system_prompt="你是一个有帮助的 Kubernetes 助手。请用简体中文清晰地总结结果。",
                messages=[
                    Message(
                        role="user",
                        content=[ContentBlock(type="text", text=synthesis_prompt)],
                    )
                ],
                max_tokens=1024,
            ))
            return response.content
        except Exception:
            return summary

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Build the master agent's system prompt with current context."""
        agents_desc = json.dumps(self.available_agents, indent=2)
        cluster_context = json.dumps(self.context.get("cluster_context", {}), indent=2)
        session_context = json.dumps(self.context.get_all("session"), indent=2)

        return render_prompt(
            MASTER_AGENT_PROMPT,
            SUB_AGENTS=agents_desc or "None registered yet",
            CLUSTER_CONTEXT=cluster_context or "Not connected to a cluster",
            SESSION_CONTEXT=session_context or "Default settings",
        )

    async def _get_all_tools(self) -> list[dict[str, Any]]:
        """Get all available tools from all sources."""
        tools: list[dict[str, Any]] = []

        # MCP server tools
        if self.mcp_client_manager:
            try:
                mcp_tools = await self.mcp_client_manager.get_aggregated_tools()
                tools.extend(mcp_tools)
            except Exception as e:
                logger.warning("failed_getting_mcp_tools", error=str(e))

        # Add delegate tool (always available)
        tools.append({
            "name": "delegate",
            "description": "将任务委派给专项子代理（k8s、故障排查、成本优化等）。"
                          "用于复杂的多步骤操作。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "The agent type to delegate to",
                        "enum": ["k8s"] + [a.get("type", "") for a in self.available_agents],
                    },
                    "task": {
                        "type": "string",
                        "description": "Natural language task description for the sub-agent",
                    },
                    "context": {
                        "type": "object",
                        "description": "Optional structured context (namespace, resource name, etc.)",
                    },
                },
                "required": ["agent", "task"],
            },
        })

        return tools

    def _should_decompose(self, request: str) -> bool:
        """Check if a request is complex enough to warrant decomposition.

        Simple read operations don't need decomposition.
        Multi-step write operations benefit from it.
        """
        # Heuristic: check for multiple action keywords
        action_keywords = [
            "deploy", "create", "delete", "scale", "update", "migrate",
            "setup", "install", "configure", "and then", "also",
        ]
        keyword_count = sum(1 for kw in action_keywords if kw in request.lower())
        return keyword_count >= 2

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset_conversation(self) -> None:
        """Explicitly reset the conversation history.

        Called when starting a brand-new session. In normal multi-turn
        conversations the history is preserved across calls to run()/run_stream().
        """
        self.conversation.clear()
        logger.info("master_agent_conversation_reset")

    async def shutdown(self) -> None:
        """Shut down the master agent."""
        logger.info("master_agent_shutdown")
        self.conversation.clear()


# ============================================================================
# Visualization extraction
# ============================================================================

def _extract_visualization(result: str, tool_name: str) -> dict[str, Any] | None:
    """Extract topology visualization data from a tool result.

    Detects if a tool result contains graph data (nodes + edges) and
    returns it in a format suitable for frontend rendering.

    Args:
        result: The tool result string (typically JSON).
        tool_name: The name of the tool that was called.

    Returns:
        Visualization data dict with 'nodes' and 'edges' keys, or None.
    """
    # Check if the tool is a topology/query tool
    viz_tools = {
        "get_topology",
        "topology",
        "list_pods",
        "list_deployments",
        "list_services",
    }

    try:
        import json as _json
        data = _json.loads(result) if isinstance(result, str) else result
    except (_json.JSONDecodeError, TypeError):
        return None

    # Case 1: Result already has topology structure
    if isinstance(data, dict) and "topology" in data:
        topo = data["topology"]
        if "nodes" in topo and "edges" in topo:
            return {
                "nodes": topo["nodes"],
                "edges": topo["edges"],
                "title": f"K8s Topology — {topo.get('namespace', '')}",
                "groups": topo.get("groups", []),
            }

    # Case 2: Auto-generate visualization from pod list results
    if tool_name in ("list_pods", "list_deployments", "list_services") and isinstance(data, dict):
        if "pods" in data and isinstance(data["pods"], list):
            return _build_viz_from_pods(data["pods"])
        if "deployments" in data and isinstance(data["deployments"], list):
            return _build_viz_from_deployments(data["deployments"])
        if "services" in data and isinstance(data["services"], list):
            return _build_viz_from_services(data["services"])

    return None


def _build_viz_from_pods(pods: list[dict]) -> dict[str, Any]:
    """Build a simple visualization from pod list data."""
    nodes: list[dict] = []
    edges: list[dict] = []
    node_names = set()

    for pod in pods:
        pname = pod.get("name", "unknown")
        ns = pod.get("namespace", "")
        phase = pod.get("phase", "Unknown")
        nid = f"pod/{ns}/{pname}"
        if nid not in node_names:
            node_names.add(nid)
            nodes.append({
                "id": nid,
                "label": pname,
                "group": "pod",
                "type": "pod",
                "metadata": {"phase": phase, "namespace": ns},
            })

    return {
        "nodes": nodes,
        "edges": edges,
        "title": f"Pods ({len(nodes)})",
        "groups": ["pod"],
    }


def _build_viz_from_deployments(deployments: list[dict]) -> dict[str, Any]:
    """Build a simple visualization from deployment list data."""
    nodes = []
    for d in deployments:
        name = d.get("name", "unknown")
        ns = d.get("namespace", "")
        replicas = d.get("replicas", 0)
        ready = d.get("ready_replicas", 0)
        nodes.append({
            "id": f"deployment/{ns}/{name}",
            "label": name,
            "group": "deployment",
            "type": "deployment",
            "metadata": {
                "replicas": replicas,
                "ready": ready,
                "namespace": ns,
                "images": d.get("images", []),
            },
        })
    return {
        "nodes": nodes,
        "edges": [],
        "title": f"Deployments ({len(nodes)})",
        "groups": ["deployment"],
    }


def _build_viz_from_services(services: list[dict]) -> dict[str, Any]:
    """Build a simple visualization from service list data."""
    nodes = []
    for s in services:
        name = s.get("name", "unknown")
        ns = s.get("namespace", "")
        stype = s.get("type", "ClusterIP")
        nodes.append({
            "id": f"service/{ns}/{name}",
            "label": name,
            "group": "service",
            "type": "service",
            "metadata": {"type": stype, "namespace": ns, "cluster_ip": s.get("cluster_ip", "")},
        })
    return {
        "nodes": nodes,
        "edges": [],
        "title": f"Services ({len(nodes)})",
        "groups": ["service"],
    }
