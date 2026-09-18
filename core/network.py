"""Graph model for the Intelligence Network view.

Pure data: builds the node and edge lists, and the vis.js physics options, with
no Streamlit import. That keeps it unit-testable and keeps the rendering call
in app.py where the component belongs.

Shape of the graph: hub nodes for each lead type and each location, with every
lead hanging off both of its hubs. A lead with an email glows brighter than one
without, so gaps are visible at a glance.
"""

from __future__ import annotations

from dataclasses import dataclass

TYPE_COLOR = "#4C8DFF"       # accent  - lead type hubs
LOCATION_COLOR = "#3DDC97"   # success - location hubs
LEAD_COLOR = "#FFB454"       # warning - a lead with an email
LEAD_DIM = "#5A6376"         # muted   - a lead with no email yet
EDGE_COLOR = "#6E7C99"   # hex, not rgba: vis.js pairs it with an opacity field
OVERFLOW_COLOR = "#2A3242"

TYPE_PREFIX = "type::"
LOCATION_PREFIX = "loc::"
LEAD_PREFIX = "lead::"
OVERFLOW_ID = "overflow::more"

DEFAULT_MAX_NODES = 120


@dataclass
class GraphData:
    nodes: list[dict]
    edges: list[dict]
    shown: int
    total: int
    hidden: int

    @property
    def signature(self) -> str:
        """Stable key for the render, so an unchanged graph is never re-simulated."""
        return f"{self.total}-{self.shown}-{len(self.nodes)}-{len(self.edges)}"


def build(rows: list[dict], max_nodes: int = DEFAULT_MAX_NODES) -> GraphData:
    """Turn lead rows into nodes and edges.

    ``max_nodes`` caps the leads drawn, not the hubs: a force simulation with a
    few hundred nodes locks up the browser, and the hubs are what carry the
    meaning. Whatever is cut is represented by a single "+N more" node so the
    picture stays honest.
    """
    leads = [row for row in rows if (row.get("name") or row.get("email"))]
    total = len(leads)
    budget = max(1, int(max_nodes))

    # Leads with an email first: those are the ones worth looking at.
    leads.sort(key=lambda row: (0 if row.get("email") else 1, row.get("name") or ""))
    shown_leads = leads[:budget]
    hidden = total - len(shown_leads)

    nodes: list[dict] = []
    edges: list[dict] = []
    seen_hubs: set[str] = set()

    type_counts: dict[str, int] = {}
    location_counts: dict[str, int] = {}
    for row in shown_leads:
        lead_type = (row.get("lead_type") or "Unclassified").strip()
        location = (row.get("location") or "Unknown").strip()
        type_counts[lead_type] = type_counts.get(lead_type, 0) + 1
        location_counts[location] = location_counts.get(location, 0) + 1

    for label, count in sorted(type_counts.items(), key=lambda kv: -kv[1]):
        node_id = TYPE_PREFIX + label
        seen_hubs.add(node_id)
        nodes.append({"id": node_id, "label": label, "color": TYPE_COLOR,
                      "size": 20 + min(22, count * 2),
                      "title": f"{label}: {count} leads", "shape": "dot"})

    for label, count in sorted(location_counts.items(), key=lambda kv: -kv[1]):
        node_id = LOCATION_PREFIX + label
        seen_hubs.add(node_id)
        nodes.append({"id": node_id, "label": label, "color": LOCATION_COLOR,
                      "size": 18 + min(20, count * 2),
                      "title": f"{label}: {count} leads", "shape": "dot"})

    for index, row in enumerate(shown_leads):
        name = (row.get("name") or row.get("email") or f"lead {index}").strip()
        node_id = f"{LEAD_PREFIX}{index}::{name[:40]}"
        has_email = bool(row.get("email"))
        detail = " | ".join(part for part in (
            row.get("email") or "no email",
            row.get("phone") or "",
            row.get("lead_type") or "",
        ) if part)
        nodes.append({
            "id": node_id,
            "label": name[:28],
            "color": LEAD_COLOR if has_email else LEAD_DIM,
            "size": 11 if has_email else 8,
            "title": f"{name}\n{detail}",
            "shape": "dot",
        })
        type_hub = TYPE_PREFIX + (row.get("lead_type") or "Unclassified").strip()
        location_hub = LOCATION_PREFIX + (row.get("location") or "Unknown").strip()
        if type_hub in seen_hubs:
            edges.append({"source": type_hub, "target": node_id})
        if location_hub in seen_hubs:
            edges.append({"source": location_hub, "target": node_id})

    if hidden > 0:
        nodes.append({"id": OVERFLOW_ID, "label": f"+{hidden} more",
                      "color": OVERFLOW_COLOR, "size": 14,
                      "title": f"{hidden} leads not drawn - raise the node cap",
                      "shape": "dot"})

    return GraphData(nodes=nodes, edges=edges, shown=len(shown_leads),
                     total=total, hidden=hidden)


def physics_options(spring_length: int = 150, repulsion: int = 18000) -> dict:
    """vis.js physics, as vis.js actually names things.

    streamlit-agraph forwards unknown keys straight through and vis.js rejects
    them with a console warning, so every key here is a real one: the
    barnesHut solver with a stabilisation pass is what makes the graph settle
    instead of drifting.

    Two entries exist purely to overwrite agraph's own defaults, which vis.js
    rejects out of the box - it sets ``arrows: "none"`` for an undirected graph
    and ``groups: None``. Config applies ``__dict__.update(**kwargs)`` last, so
    passing them here wins.
    """
    return {
        "groups": {},
        # The graph first mounts inside a Streamlit tab that is not the visible
        # one, so vis.js fits the view to a zero-width canvas and everything
        # ends up enormous once the tab opens. autoResize makes it re-fit when
        # the container finally has a size.
        "autoResize": True,
        "physics": {
            "enabled": True,
            "solver": "barnesHut",
            "barnesHut": {
                "gravitationalConstant": -abs(repulsion),
                "centralGravity": 0.28,
                "springLength": spring_length,
                "springConstant": 0.045,
                "damping": 0.28,
                "avoidOverlap": 0.15,
            },
            "stabilization": {"enabled": True, "iterations": 220,
                              "updateInterval": 25, "fit": True},
            "minVelocity": 0.6,
        },
        "nodes": {
            "borderWidth": 0,
            "font": {"color": "#C6CEDC", "size": 13, "face": "Inter, sans-serif"},
            "shadow": {"enabled": True, "color": "rgba(0,0,0,0.45)", "size": 12},
        },
        "edges": {
            # inherit=False or vis.js tints every edge with its endpoint
            # colours instead of using the one set here.
            "color": {"color": EDGE_COLOR, "highlight": "#4C8DFF",
                      "hover": "#4C8DFF", "inherit": False, "opacity": 0.55},
            "width": 1,
            "smooth": {"enabled": True, "type": "continuous"},
            "arrows": {"to": {"enabled": False}, "from": {"enabled": False}},
        },
        "interaction": {"hover": True, "tooltipDelay": 120,
                        "navigationButtons": False, "zoomView": True},
    }
