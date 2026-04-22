"""
Integration-lite tests for agents — verify instantiation and LLM routing.
These do NOT make real LLM calls (agents are not kicked off).
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestAgentInstantiation:
    def test_all_agents_importable(self):
        import agents
        for name in [
            "daily_trend_agent", "multiday_trend_agent", "news_analyst_agent",
            "futurist_agent", "project_manager_agent", "trader_agent",
        ]:
            assert hasattr(agents, name), f"Missing agent: {name}"

    def test_resilient_agents_use_gemma_only(self):
        from agents import ResilientAgent, daily_trend_agent, futurist_agent
        assert isinstance(daily_trend_agent, ResilientAgent)
        assert isinstance(futurist_agent, ResilientAgent)
        assert "gemma" in daily_trend_agent.llm.model.lower()

    def test_trader_uses_local_model(self):
        from agents import trader_agent
        assert "ollama" in trader_agent.llm.model.lower() or "gemma" in trader_agent.llm.model.lower()

    def test_manager_allows_delegation(self):
        from agents import project_manager_agent
        assert project_manager_agent.allow_delegation is True


class TestTaskDefinitions:
    def test_all_tasks_importable(self):
        import tasks
        for name in [
            "daily_trend_task", "multiday_trend_task", "news_task",
            "futurist_task", "manager_task", "trader_task",
        ]:
            assert hasattr(tasks, name), f"Missing task: {name}"

    def test_futurist_task_has_context(self):
        from tasks import futurist_task
        assert futurist_task.context is not None
        assert len(futurist_task.context) >= 2

    def test_manager_task_has_full_context(self):
        from tasks import manager_task
        assert len(manager_task.context) >= 4

    def test_trader_task_depends_on_manager(self):
        from tasks import trader_task, manager_task
        assert manager_task in trader_task.context


