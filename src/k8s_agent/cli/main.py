"""CLI entry point for k8s-agent.

Commands:
  k8s-agent serve        Start the K8s MCP server
  k8s-agent run          Interactive agent session
  k8s-agent web          Start the Web UI (FastAPI)
  k8s-agent skill        Manage skills (list, validate, init)
  k8s-agent config       Manage configuration (show, init)
"""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

import typer

app = typer.Typer(
    name="k8s-agent",
    help="LLM-powered Kubernetes Agent with multi-agent collaboration",
    add_completion=False,
)


@app.command()
def serve(
    transport: str = typer.Option("stdio", help="Transport: stdio or http"),
    host: str = typer.Option("0.0.0.0", help="HTTP host"),
    port: int = typer.Option(3100, help="HTTP port"),
) -> None:
    """Start the Kubernetes MCP server.

    Stdio mode: for use with Claude Desktop or other MCP clients.
    HTTP mode: for remote access or integration testing.
    """
    from k8s_agent.mcp_server.server import run_stdio_server, run_http_server

    if transport == "http":
        typer.echo(f"Starting K8s MCP Server on http://{host}:{port}")
        asyncio.run(run_http_server(host, port))
    else:
        typer.echo("Starting K8s MCP Server (stdio mode)")
        asyncio.run(run_stdio_server())


@app.command()
def run(
    request: Optional[str] = typer.Argument(None, help="One-shot request. If omitted, starts interactive REPL."),
    model: str = typer.Option("deepseek-chat", help="Model name: deepseek-chat (V4 Pro), deepseek-reasoner (R1), claude-sonnet-4-5-20250929"),
    provider: str = typer.Option("deepseek", help="LLM provider: anthropic, deepseek, auto"),
    namespace: str = typer.Option("default", help="Default Kubernetes namespace"),
) -> None:
    """Run the Kubernetes Agent.

    Without arguments, starts an interactive REPL.
    With a request argument, processes it and exits.

    Examples:
      k8s-agent run  # Interactive REPL (DeepSeek V4 Pro by default)
      k8s-agent run --provider anthropic --model claude-sonnet-4-5-20250929
      k8s-agent run --provider deepseek --model deepseek-chat
      k8s-agent run "List all pods in default namespace"
    """
    from k8s_agent.core.orchestrator import MasterAgent
    from k8s_agent.core.communication.message_bus import MessageBus
    from k8s_agent.core.communication.context import SharedContext
    from k8s_agent.core.mcp.client_manager import MCPClientManager
    from k8s_agent.llm import create_provider
    from k8s_agent.shared.config import get_settings
    from k8s_agent.shared.logging import setup_logging

    setup_logging(level="info", output_format="pretty")

    async def _run() -> None:
        settings = get_settings()
        settings.llm.model = model

        llm = create_provider(provider_type=provider, model=model)
        message_bus = MessageBus()
        context = SharedContext()
        context.set("current_namespace", namespace, scope="session")

        mcp_manager = MCPClientManager()

        # Try to connect to K8s MCP server
        try:
            from k8s_agent.core.mcp.client_manager import MCPServerConfig
            await mcp_manager.connect(MCPServerConfig(
                name="k8s-agent-mcp-server",
                transport="stdio",
                command=sys.executable,
                args=["-m", "k8s_agent.mcp_server.server"],
            ))
        except Exception as e:
            typer.echo(f"Warning: Could not connect to K8s MCP server: {e}")
            typer.echo("Running with direct K8s API fallback.")

        master = MasterAgent(
            llm_provider=llm,
            message_bus=message_bus,
            context=context,
            mcp_client_manager=mcp_manager,
        )
        await master.initialize()

        if request:
            typer.echo(f"\n🤖 Processing: {request}\n")
            response = await master.run(request)
            typer.echo(response)
        else:
            # Interactive REPL
            typer.echo("🤖 Kubernetes Agent REPL")
            typer.echo("Type 'exit' or 'quit' to stop, 'clear' to reset conversation.\n")

            while True:
                try:
                    user_input = typer.prompt(">")
                except (EOFError, KeyboardInterrupt):
                    typer.echo("\nGoodbye!")
                    break

                user_input = user_input.strip()
                if not user_input:
                    continue
                if user_input.lower() in ("exit", "quit"):
                    typer.echo("Goodbye!")
                    break
                if user_input.lower() == "clear":
                    master.conversation.clear()
                    typer.echo("Conversation cleared.")
                    continue

                typer.echo()
                try:
                    response = await master.run(user_input)
                    typer.echo(response)
                    typer.echo()
                except Exception as e:
                    typer.echo(f"❌ Error: {e}")

        await mcp_manager.disconnect_all()

    asyncio.run(_run())


@app.command()
def web(
    host: str = typer.Option("0.0.0.0", help="Web UI host"),
    port: int = typer.Option(8080, help="Web UI port"),
    reload: bool = typer.Option(False, help="Enable auto-reload for development"),
) -> None:
    """Start the K8s Agent Web UI.

    Provides a web-based chat interface for interacting with the agent.
    Includes streaming responses, tool call visualization, and conversation history.
    """
    import uvicorn
    from k8s_agent.api.app import create_app

    app = create_app()
    typer.echo(f"🚀 K8s Agent Web UI starting at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, reload=reload)


@app.command()
def skill(
    action: str = typer.Argument("list", help="Action: list, validate, init"),
    path: str = typer.Option("", help="Skill path (for validate/init)"),
) -> None:
    """Manage skills: list, validate, or initialize a new skill."""
    import json

    if action == "list":
        from k8s_agent.skills.builtin import BUILTIN_SKILLS

        typer.echo("📦 Built-in Skills:\n")
        for skill_cls in BUILTIN_SKILLS:
            skill = skill_cls()
            manifest = skill.get_manifest()
            tools = skill.get_tools()
            typer.echo(f"  {manifest.name} (v{manifest.version})")
            typer.echo(f"    {manifest.description}")
            typer.echo(f"    Tools: {', '.join(t.name for t in tools)}")
            typer.echo()

        typer.echo("💡 To create a custom skill, run: k8s-agent skill init")

    elif action == "init":
        target_path = path or "./my-skill"
        import os
        os.makedirs(target_path, exist_ok=True)

        # Create skill.json
        skill_json = {
            "name": "my-custom-skill",
            "version": "1.0.0",
            "description": "My custom Kubernetes skill",
            "author": "your-name",
            "keywords": ["kubernetes"],
            "agent_type": "tool-only",
            "capabilities": ["custom_operation"],
        }
        with open(os.path.join(target_path, "skill.json"), "w") as f:
            json.dump(skill_json, f, indent=2)

        # Create index.py template
        index_py = '''"""My custom K8s skill."""

import json
from k8s_agent.skills.interface import ISkill, SkillContext, SkillTool
from k8s_agent.skills.manifest import SkillManifest


class MyCustomSkill(ISkill):
    def get_manifest(self):
        return SkillManifest(
            name="my-custom-skill",
            version="1.0.0",
            description="My custom Kubernetes skill",
            capabilities=["custom_operation"],
        )

    def get_tools(self):
        return [
            SkillTool(
                name="my_tool",
                description="Does something useful in Kubernetes",
                input_schema={
                    "type": "object",
                    "properties": {
                        "param": {"type": "string", "description": "A parameter"},
                    },
                    "required": ["param"],
                },
                handler=self._my_tool,
            ),
        ]

    async def _my_tool(self, param: str) -> str:
        return json.dumps({"result": f"Executed with {param}"})


def create_skill():
    return MyCustomSkill()
'''
        with open(os.path.join(target_path, "index.py"), "w") as f:
            f.write(index_py)

        typer.echo(f"✅ Skill template created at {target_path}/")
        typer.echo("   Edit skill.json and index.py to implement your skill.")
        typer.echo("   Then register it in your k8s-agent config to use it.")

    elif action == "validate":
        from k8s_agent.skills.loader import SkillLoader
        from k8s_agent.skills.interface import SkillContext
        from k8s_agent.shared.logging import get_logger

        async def _validate():
            ctx = SkillContext(logger=get_logger("skill-validate"))
            loader = SkillLoader(ctx)

            if path:
                import os
                manifest_path = os.path.join(path, "skill.json")
                if not os.path.exists(manifest_path):
                    typer.echo(f"❌ No skill.json found at {manifest_path}")
                    return
                with open(manifest_path) as f:
                    data = json.load(f)
                manifest = SkillManifest.from_dict(data)
                errors = manifest.validate()
            else:
                discovered = loader.discover()
                if not discovered:
                    typer.echo("No skills found.")
                    return
                _, manifest = discovered[0]
                errors = manifest.validate()

            if errors:
                typer.echo(f"❌ Validation errors in {manifest.name}:")
                for e in errors:
                    typer.echo(f"   - {e}")
            else:
                typer.echo(f"✅ Skill '{manifest.name}' is valid.")

        asyncio.run(_validate())

    else:
        typer.echo(f"Unknown action: {action}. Use: list, validate, init")


@app.command()
def config(
    action: str = typer.Argument("show", help="Action: show, init"),
) -> None:
    """Manage configuration: show current or initialize a config file."""
    import yaml

    if action == "show":
        from k8s_agent.shared.config import get_settings
        settings = get_settings()
        typer.echo(yaml.dump(settings.model_dump(), default_flow_style=False))

    elif action == "init":
        config_path = "./k8s-agent.config.yaml"
        config_content = """# K8s Agent Configuration
kubernetes:
  auth_mode: kubeconfig  # kubeconfig | in-cluster | token
  kubeconfig_path: ~/.kube/config
  context: null

llm:
  provider: anthropic
  api_key: ${ANTHROPIC_API_KEY}
  model: claude-sonnet-4-5-20250929
  max_tokens: 4096
  temperature: 0.2

mcp_server:
  transport: stdio
  http_host: 0.0.0.0
  http_port: 3100

agent:
  max_iterations: 15
  task_timeout_ms: 300000

skills:
  paths:
    - ~/.k8s-agent/skills
    - ./.k8s-agent/skills

logging:
  level: info
  format: pretty
"""
        with open(config_path, "w") as f:
            f.write(config_content)
        typer.echo(f"✅ Config file created at {config_path}")

    else:
        typer.echo(f"Unknown action: {action}. Use: show, init")


def main() -> None:
    """Entry point for k8s-agent CLI."""
    app()


if __name__ == "__main__":
    main()
