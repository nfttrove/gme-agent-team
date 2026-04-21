"""
Task data injection for Gemma-only execution.
Injects pre-fetched data into task descriptions so agents don't need tool use.
"""
from local_data_fetcher import LocalDataFetcher, inject_data_into_task
from crewai import Task


def prepare_task_with_injection(task: Task) -> Task:
    """Inject data into task description to eliminate need for tool use."""
    if not task.description:
        return task

    # Inject relevant data based on task description
    task.description = inject_data_into_task(task.description)

    return task


def prepare_crew_tasks(tasks: list) -> list:
    """Prepare all tasks in a crew with data injection."""
    return [prepare_task_with_injection(t) for t in tasks]
