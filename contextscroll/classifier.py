"""Pure semantic UI classification.

The session helper converts AT-SPI objects into :class:`SemanticNode` values.
Keeping policy in this dependency-free module makes the important safety
decision easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable, Mapping


class Decision(StrEnum):
    NATIVE = "native"
    SCROLL = "scroll"
    UNKNOWN = "unknown"


def normalize(value: str) -> str:
    return " ".join(value.lower().replace("_", " ").replace("-", " ").split())


@dataclass(frozen=True, slots=True)
class SemanticNode:
    role: str
    states: frozenset[str] = field(default_factory=frozenset)
    actions: frozenset[str] = field(default_factory=frozenset)
    attributes: Mapping[str, str] = field(default_factory=dict)
    name: str = ""

    @classmethod
    def create(
        cls,
        role: str,
        states: Iterable[str] = (),
        actions: Iterable[str] = (),
        attributes: Mapping[str, str] | None = None,
        name: str = "",
    ) -> "SemanticNode":
        return cls(
            role=normalize(role),
            states=frozenset(normalize(item) for item in states),
            actions=frozenset(normalize(item) for item in actions),
            attributes={
                normalize(str(key)): normalize(str(value))
                for key, value in (attributes or {}).items()
            },
            name=name[:160],
        )


# Controls for which a middle click can have native meaning. This deliberately
# includes tabs, links and editable fields before considering scroll ancestors.
NATIVE_ROLES = {
    "button",
    "check box",
    "check menu item",
    "combo box",
    "date editor",
    "desktop icon",
    "dial",
    "editbar",
    "entry",
    "file chooser",
    "icon",
    "link",
    "menu",
    "menu bar",
    "menu item",
    "page tab",
    "page tab list",
    "password text",
    "popup menu",
    "push button menu",
    "radio button",
    "radio menu item",
    "rating",
    "scroll bar",
    "slider",
    "spin button",
    "switch",
    "tearoff menu item",
    "toggle button",
    "tool bar",
    "tree item",
}

SCROLL_ROLES = {
    "article",
    "canvas",
    "description list",
    "directory pane",
    "document email",
    "document frame",
    "document presentation",
    "document spreadsheet",
    "document text",
    "document web",
    "drawing area",
    "html container",
    "list",
    "list box",
    "log",
    "page",
    "scroll pane",
    "table",
    "tree",
    "tree table",
    "viewport",
}

# Chromium-family browsers do not consistently include a ``document web`` or
# ``scroll pane`` object in the ancestry returned by GetAccessibleAtPoint.
# Their page body commonly bottoms out at one of these content roles instead.
# This fallback is restricted to known browser application names so that a
# generic section in a desktop application is not intercepted.
BROWSER_CONTENT_ROLES = {
    "article",
    "canvas",
    "heading",
    "image",
    "paragraph",
    "section",
    "static",
    "text",
    "video",
}

BROWSER_APPLICATION_MARKERS = {
    "brave",
    "chromium",
    "firefox",
    "floorp",
    "google chrome",
    "librewolf",
    "microsoft edge",
    "opera",
    "vivaldi",
    "zen browser",
}

ACTIVATION_ACTIONS = {
    "activate",
    "click",
    "close",
    "jump",
    "open",
    "press",
    "select",
}

ACTION_CONTAINER_ROLES = {
    "application",
    "document frame",
    "document web",
    "frame",
    "internal frame",
    "panel",
    "scroll pane",
    "section",
    "window",
}


def is_native_target(node: SemanticNode) -> bool:
    if node.role in NATIVE_ROLES:
        return True
    if "editable" in node.states:
        return True
    if node.attributes.get("tag") == "a":
        return True
    xml_roles = set(node.attributes.get("xml roles", "").split())
    if {"button", "link", "tab"} & xml_roles:
        return True
    # Toolkits commonly expose activate/focus actions on window frames and
    # other containers. Those actions do not make the content at the pointer
    # an interactive target.
    if node.role in ACTION_CONTAINER_ROLES:
        return False
    return bool(node.actions & ACTIVATION_ACTIONS)


def is_browser_application(application: str) -> bool:
    name = normalize(application)
    return any(marker in name for marker in BROWSER_APPLICATION_MARKERS)


def classify_chain(
    nodes: Iterable[SemanticNode], application: str = ""
) -> Decision:
    """Classify a deepest-first accessible ancestry chain.

    Native actions have precedence over a scrollable ancestor. A text node
    inside a link must therefore remain a native click even though its document
    ancestor is scrollable.
    """

    chain = tuple(nodes)
    if any(is_native_target(node) for node in chain):
        return Decision.NATIVE
    if any(node.role in SCROLL_ROLES for node in chain):
        return Decision.SCROLL
    if is_browser_application(application) and any(
        node.role in BROWSER_CONTENT_ROLES for node in chain
    ):
        return Decision.SCROLL
    return Decision.UNKNOWN
