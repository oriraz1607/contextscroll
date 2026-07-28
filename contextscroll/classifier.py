"""Pure semantic UI classification.

The session helper converts AT-SPI objects into :class:`SemanticNode` values.
Keeping policy in this dependency-free module makes the important safety
decision easy to test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable, Mapping

MAX_RULES = 128
MAX_RULES_BYTES = 65_536


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
    hyperlink_target: bool = False

    @classmethod
    def create(
        cls,
        role: str,
        states: Iterable[str] = (),
        actions: Iterable[str] = (),
        attributes: Mapping[str, str] | None = None,
        name: str = "",
        hyperlink_target: bool = False,
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
            hyperlink_target=bool(hyperlink_target),
        )


@dataclass(frozen=True, slots=True)
class ContextRule:
    """One ordered user override for the built-in semantic classifier."""

    decision: Decision
    application: str = ""
    role: str = ""
    states: frozenset[str] = field(default_factory=frozenset)
    actions: frozenset[str] = field(default_factory=frozenset)
    name: str = ""
    enabled: bool = True

    def matches(
        self, nodes: tuple[SemanticNode, ...], application: str
    ) -> bool:
        if not self.enabled:
            return False
        if self.application and self.application not in normalize(application):
            return False
        if not any((self.role, self.states, self.actions, self.name)):
            return bool(self.application)
        for node in nodes:
            if self.role and node.role != self.role:
                continue
            if self.states and not self.states.issubset(node.states):
                continue
            if self.actions and not self.actions.issubset(node.actions):
                continue
            if self.name and self.name not in normalize(node.name):
                continue
            return True
        return False


def parse_context_rules(text: str) -> tuple[tuple[ContextRule, ...], list[str]]:
    """Validate bounded preferences JSON while retaining valid rules."""

    if len(text.encode("utf-8")) > MAX_RULES_BYTES:
        return (), [f"rules exceed the {MAX_RULES_BYTES}-byte limit"]
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as error:
        return (), [f"rules are not valid JSON: {error}"]
    if not isinstance(payload, list):
        return (), ["rules must be a JSON array"]
    errors: list[str] = []
    rules: list[ContextRule] = []
    for index, item in enumerate(payload[:MAX_RULES]):
        prefix = f"rule {index + 1}"
        if not isinstance(item, dict):
            errors.append(f"{prefix} is not an object")
            continue

        def string(name: str, maximum: int) -> str:
            value = item.get(name, "")
            if not isinstance(value, str):
                raise ValueError(f"{name} must be text")
            return normalize(value)[:maximum]

        def string_set(name: str) -> frozenset[str]:
            value = item.get(name, [])
            if not isinstance(value, list) or any(
                not isinstance(entry, str) for entry in value
            ):
                raise ValueError(f"{name} must be a text array")
            return frozenset(
                normalized
                for entry in value[:32]
                if (normalized := normalize(entry)[:80])
            )

        try:
            decision = Decision(item.get("decision", ""))
            if decision not in (Decision.NATIVE, Decision.SCROLL):
                raise ValueError("decision must be native or scroll")
            enabled = item.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be true or false")
            rule = ContextRule(
                decision=decision,
                application=string("application", 120),
                role=string("role", 80),
                states=string_set("states"),
                actions=string_set("actions"),
                name=string("name", 160),
                enabled=enabled,
            )
            if not any(
                (
                    rule.application,
                    rule.role,
                    rule.states,
                    rule.actions,
                    rule.name,
                )
            ):
                raise ValueError("at least one matcher is required")
        except (ValueError, TypeError) as error:
            errors.append(f"{prefix}: {error}")
            continue
        rules.append(rule)
    if len(payload) > MAX_RULES:
        errors.append(f"only the first {MAX_RULES} rules were loaded")
    return tuple(rules), errors


def matching_rule(
    nodes: Iterable[SemanticNode],
    application: str,
    rules: Iterable[ContextRule],
) -> Decision | None:
    chain = tuple(nodes)
    for rule in rules:
        if rule.matches(chain, application):
            return rule.decision
    return None


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
    "list item",
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
    "table cell",
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
    "landmark",
    "list item",
    "paragraph",
    "section",
    "static",
    "table cell",
    "text",
    "video",
}

# HTML list items and table cells are structural page content, unlike their
# interactive counterparts in desktop list and grid widgets. A real browser
# link, control, or editable descendant is still recognized independently.
BROWSER_STRUCTURAL_CONTENT_ROLES = {
    "list item",
    "table cell",
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

LIBREOFFICE_APPLICATION_MARKERS = {
    "libreoffice",
    "soffice",
}

VIEWER_APPLICATION_MARKERS = {
    "calibre ebook viewer",
    "document viewer",
    "eye of gnome",
    "evince",
    "foliate",
    "image viewer",
    "loupe",
    "okular",
    "papers",
    "yelp",
    "zathura",
}

VIEWER_EXACT_NAMES = {
    "help",
}

VIEWER_CONTENT_ROLES = {
    "article",
    "canvas",
    "drawing area",
    "html container",
    "image",
    "page",
    "panel",
    "paragraph",
    "section",
    "static",
    "text",
    "viewport",
}

FILE_MANAGER_APPLICATION_MARKERS = {
    "caja",
    "dolphin",
    "nautilus",
    "nemo",
    "pcmanfm",
    "thunar",
}

FILE_MANAGER_EXACT_NAMES = {
    "files",
}

FILE_MANAGER_CONTENT_ROLES = {
    "directory pane",
    "layered pane",
    "list",
    "list box",
    "panel",
    "section",
    "table",
    "tree",
    "viewport",
}

ACTION_CONTAINER_ROLES = {
    "application",
    "document frame",
    "document presentation",
    "document spreadsheet",
    "document text",
    "document web",
    "frame",
    "internal frame",
    "page",
    "scroll pane",
    "window",
}

PASSIVE_ACTIONS = {
    # Chromium exposes these on almost every web object, including empty
    # background divs. Neither proves that the object itself performs the
    # page action a middle click should preserve.
    "clickancestor",
    "showcontextmenu",
}

MAX_DIRECT_ACTION_ANCESTORS = 4


def is_explicit_native_target(
    node: SemanticNode,
    ignored_roles: frozenset[str] = frozenset(),
) -> bool:
    if node.hyperlink_target:
        return True
    if node.role in NATIVE_ROLES and node.role not in ignored_roles:
        return True
    if node.attributes.get("tag") == "a":
        return True
    xml_roles = set(node.attributes.get("xml roles", "").split())
    if {"button", "link", "tab"} & xml_roles:
        return True
    return False


def has_direct_action(node: SemanticNode) -> bool:
    # Toolkits commonly expose actions on document roots, window frames and
    # scroll containers. Those broad structural actions do not mean the item
    # directly beneath the pointer is interactive.
    if node.role in ACTION_CONTAINER_ROLES:
        return False
    # AT-SPI actions are the closest cross-toolkit answer to "would clicking
    # this item do something?". Preserve native middle click for concrete
    # nearby actions, including custom controls and application-specific
    # actions whose names are not known in advance.
    return bool(node.actions - PASSIVE_ACTIONS)


def chain_has_native_target(
    chain: tuple[SemanticNode, ...],
    *,
    include_editable: bool,
    include_actions: bool,
    ignored_roles: frozenset[str] = frozenset(),
) -> bool:
    if any(
        is_explicit_native_target(node, ignored_roles)
        or (include_editable and "editable" in node.states)
        for node in chain
    ):
        return True
    if not include_actions:
        return False
    # A child such as text may sit inside the actionable object. Limit action
    # inference to the nearest few ancestors so a page-level click handler
    # does not turn every blank area into a native middle click.
    return any(
        has_direct_action(node)
        for node in chain[:MAX_DIRECT_ACTION_ANCESTORS]
    )


def is_browser_application(application: str) -> bool:
    name = normalize(application)
    return any(marker in name for marker in BROWSER_APPLICATION_MARKERS)


def application_matches(
    application: str,
    markers: set[str],
    exact_names: set[str] | None = None,
) -> bool:
    name = normalize(application)
    return name in (exact_names or set()) or any(
        marker in name for marker in markers
    )


def is_libreoffice_writer(
    chain: tuple[SemanticNode, ...], application: str
) -> bool:
    name = normalize(application)
    return (
        any(marker in name for marker in LIBREOFFICE_APPLICATION_MARKERS)
        and any(node.role == "document text" for node in chain)
    )


def classify_chain(
    nodes: Iterable[SemanticNode],
    application: str = "",
    rules: Iterable[ContextRule] = (),
) -> Decision:
    """Classify a deepest-first accessible ancestry chain.

    Native actions have precedence over a scrollable ancestor. A text node
    inside a link must therefore remain a native click even though its document
    ancestor is scrollable.
    """

    chain = tuple(nodes)
    override = matching_rule(chain, application, rules)
    if override is not None:
        return override
    browser = is_browser_application(application)
    if is_libreoffice_writer(chain, application):
        # Writer exposes its document body as editable text. A blanket
        # editable rule would preserve primary-selection paste and prevent
        # autoscroll everywhere in the page. Ignore editable only within the
        # Writer document ancestry; real controls and links remain native.
        if chain_has_native_target(
            chain,
            include_editable=False,
            include_actions=True,
        ):
            return Decision.NATIVE
        return Decision.SCROLL
    if chain_has_native_target(
        chain,
        include_editable=True,
        # Chromium's action interface describes left-click and context-menu
        # behavior but not whether middle-click has a native meaning. Generic
        # browser actions therefore cannot safely override autoscroll.
        include_actions=not browser,
        ignored_roles=(
            frozenset(BROWSER_STRUCTURAL_CONTENT_ROLES)
            if browser
            else frozenset()
        ),
    ):
        return Decision.NATIVE
    if any(node.role in SCROLL_ROLES for node in chain):
        return Decision.SCROLL
    if browser and any(
        node.role in BROWSER_CONTENT_ROLES for node in chain
    ):
        return Decision.SCROLL
    if application_matches(
        application,
        VIEWER_APPLICATION_MARKERS,
        VIEWER_EXACT_NAMES,
    ) and any(node.role in VIEWER_CONTENT_ROLES for node in chain):
        return Decision.SCROLL
    if application_matches(
        application,
        FILE_MANAGER_APPLICATION_MARKERS,
        FILE_MANAGER_EXACT_NAMES,
    ) and any(node.role in FILE_MANAGER_CONTENT_ROLES for node in chain):
        return Decision.SCROLL
    return Decision.UNKNOWN
