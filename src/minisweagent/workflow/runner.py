"""Run one workflow subagent using the parent agent's model, tools, and environment."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable

from minisweagent.workflow.models import AgentRunResult, WorkflowError

logger = logging.getLogger("workflow")


def build_workflow_runner(parent) -> Callable[..., AgentRunResult]:
    def run(
        prompt: str,
        *,
        system: str,
        result_schema: dict | None = None,
        max_steps: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> AgentRunResult:
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.extensions.subagents import (
            BUILTIN_PROFILES,
            _build_sub_model,
            _subagent_registry,
        )
        from minisweagent.workflow.tools import build_escalate_tool, build_submit_result_tool

        profile = getattr(parent, "_profiles", {}).get("general-purpose") or BUILTIN_PROFILES["general-purpose"]
        sub_model = _build_sub_model(parent, profile, None)
        if sub_model is None:
            raise WorkflowError("The configured model cannot be created for a workflow subagent")
        setter = getattr(sub_model, "set_extra_tools", None)
        if setter is None:
            raise WorkflowError("Workflow subagents require a tool-call model (use model_class=litellm)")

        agent_id = f"wf-{uuid.uuid4().hex[:6]}"
        tools = _subagent_registry(parent, profile, agent_id)
        # The tool-call loop cannot end on a plain assistant message, so every
        # workflow ask (typed or not) finishes through submit_result.
        tools.add(build_submit_result_tool(result_schema))
        tools.add(build_escalate_tool())
        setter(tools.specs())

        sub_config = parent.config.model_copy(deep=True)
        sub_config.system_template = system
        sub_config.instance_template = "{{task}}"
        sub_config.output_path = None
        sub_config.subagents.enabled = False
        sub_config.skills.enabled = False
        sub_config.mcp.enabled = False
        sub_config.browser.enabled = False
        sub_config.vision.enabled = False
        sub_config.plugins.enabled = False
        sub_config.automations.enabled = False
        sub_config.session.enabled = False
        sub_config.hooks.enabled = False
        sub_config.background.enabled = False
        sub_config.bus.enabled = False
        sub_config.workflow.enabled = False
        sub_config.tools.read_only = False
        sub_config.prompts.files = []
        sub_config.prompts.dirs = []
        sub_config.step_limit = max_steps or parent.config.workflow.max_steps_per_agent or 0

        subagent = DefaultAgent(
            sub_model,
            parent.env,
            tool_registry=tools,
            prompt_sections=[],
            background_manager=getattr(parent, "_background", None),
            hook_runner=getattr(parent, "_hooks", None),
            agent_bus=getattr(parent, "_bus", None),
            agent_id=agent_id,
            cancel_event=cancel_event,
            **sub_config.model_dump(exclude={"agent_class"}),
        )
        subagent._subagent_depth = getattr(parent, "_subagent_depth", 0) + 1  # noqa: SLF001
        subagent._owns_mcp = False  # noqa: SLF001
        result = subagent.run(prompt)

        parent.cost += subagent.cost
        parent.n_calls += subagent.n_calls
        parent.n_compaction_calls += subagent.n_compaction_calls

        exit_status = str(result.get("exit_status") or "unknown")
        text = str(result.get("submission") or result.get("content") or "")
        value = result.get("result")
        escalation = result.get("escalation") or {}
        if value is None and exit_status != "Escalated":
            raise WorkflowError(
                "The ask ended without a submit_result call, so there is no result. "
                f"Final text: {text[:500]}"
            )
        return AgentRunResult(
            text=text,
            value=value,
            exit_status=exit_status,
            cost=subagent.cost,
            n_calls=subagent.n_calls,
            n_compaction_calls=subagent.n_compaction_calls,
            escalation=escalation,
        )

    return run
