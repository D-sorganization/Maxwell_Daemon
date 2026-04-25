"""Built-in task graph templates."""

from __future__ import annotations

from maxwell_daemon.graphs import AgentRole
from maxwell_daemon.graphs.templates import (
    TaskGraphTemplate,
    TemplateSelectionInput,
    build_template_graph,
    select_template,
)


def test_low_risk_small_inputs_select_micro_delivery() -> None:
    selection = TemplateSelectionInput(risk="low", estimated_steps=1)

    assert select_template(selection) is TaskGraphTemplate.MICRO_DELIVERY


def test_normal_inputs_select_standard_delivery() -> None:
    selection = TemplateSelectionInput(risk="normal", estimated_steps=3)

    assert select_template(selection) is TaskGraphTemplate.STANDARD_DELIVERY


def test_security_sensitive_inputs_select_security_sensitive_delivery() -> None:
    selection = TemplateSelectionInput(
        risk="normal",
        estimated_steps=1,
        security_sensitive=True,
    )

    assert select_template(selection) is TaskGraphTemplate.SECURITY_SENSITIVE_DELIVERY


def test_micro_delivery_template_is_single_implementer_node() -> None:
    graph = build_template_graph(TaskGraphTemplate.MICRO_DELIVERY)

    assert graph.id == "micro-delivery"
    assert [node.id for node in graph.nodes] == ["implement"]
    assert graph.nodes[0].role is AgentRole.IMPLEMENTER


def test_standard_delivery_template_orders_planning_implementation_and_review() -> None:
    graph = build_template_graph(TaskGraphTemplate.STANDARD_DELIVERY)

    assert [node.id for node in graph.nodes_in_dependency_order()] == [
        "plan",
        "implement",
        "qa",
        "review",
    ]
    assert graph.node_by_id("qa").depends_on == ("implement",)
    assert graph.node_by_id("review").role is AgentRole.REVIEWER


def test_security_sensitive_template_includes_security_review_before_final_review() -> None:
    graph = build_template_graph(TaskGraphTemplate.SECURITY_SENSITIVE_DELIVERY)

    assert [node.id for node in graph.nodes_in_dependency_order()] == [
        "plan",
        "implement",
        "qa",
        "security-review",
        "review",
    ]
    assert graph.node_by_id("security-review").role is AgentRole.SECURITY_REVIEWER
    assert graph.node_by_id("review").depends_on == ("security-review",)
