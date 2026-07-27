import unittest

from contextscroll.classifier import Decision, SemanticNode, classify_chain


class ClassifierTests(unittest.TestCase):
    def node(self, role, **kwargs):
        return SemanticNode.create(role, **kwargs)

    def test_browser_tab_is_native(self):
        chain = [
            self.node("page tab", name="Documentation"),
            self.node("page tab list"),
            self.node("frame"),
        ]
        self.assertEqual(classify_chain(chain), Decision.NATIVE)

    def test_link_inside_document_is_native(self):
        chain = [
            self.node("text"),
            self.node("link", actions=["jump"]),
            self.node("document web"),
        ]
        self.assertEqual(classify_chain(chain), Decision.NATIVE)

    def test_editable_text_is_native(self):
        chain = [
            self.node("text", states=["editable"]),
            self.node("document web"),
        ]
        self.assertEqual(classify_chain(chain), Decision.NATIVE)

    def test_web_document_is_scroll(self):
        chain = [
            self.node("paragraph"),
            self.node("document web"),
            self.node("scroll pane"),
        ]
        self.assertEqual(classify_chain(chain), Decision.SCROLL)

    def test_window_activation_does_not_override_web_document(self):
        chain = [
            self.node("paragraph"),
            self.node("document web"),
            self.node("frame", actions=["activate"]),
        ]
        self.assertEqual(classify_chain(chain), Decision.SCROLL)

    def test_unknown_toolbar_decoration_is_safe_unknown(self):
        self.assertEqual(
            classify_chain([self.node("separator"), self.node("frame")]),
            Decision.UNKNOWN,
        )

    def test_generic_desktop_panel_is_not_assumed_scrollable(self):
        self.assertEqual(
            classify_chain([self.node("panel"), self.node("frame")]),
            Decision.UNKNOWN,
        )

    def test_aria_link_attribute_is_native(self):
        chain = [
            self.node("static", attributes={"xml-roles": "link"}),
            self.node("document web"),
        ]
        self.assertEqual(classify_chain(chain), Decision.NATIVE)

    def test_chromium_section_is_browser_content(self):
        chain = [
            self.node("section"),
            self.node("panel"),
            self.node("frame"),
        ]
        for application in (
            "Brave Browser",
            "Chromium",
            "Google Chrome",
            "Microsoft Edge",
            "Vivaldi",
            "Opera",
        ):
            with self.subTest(application=application):
                self.assertEqual(
                    classify_chain(chain, application),
                    Decision.SCROLL,
                )

    def test_chromium_tab_remains_native(self):
        chain = [
            self.node("page tab", actions=["click"]),
            self.node("page tab list"),
            self.node("frame"),
        ]
        self.assertEqual(
            classify_chain(chain, "Brave Browser"),
            Decision.NATIVE,
        )

    def test_chromium_link_remains_native(self):
        chain = [
            self.node("text"),
            self.node("link", actions=["jump"]),
            self.node("section"),
            self.node("frame"),
        ]
        self.assertEqual(
            classify_chain(chain, "Google Chrome"),
            Decision.NATIVE,
        )

    def test_generic_section_outside_browser_remains_unknown(self):
        chain = [self.node("section"), self.node("frame")]
        self.assertEqual(
            classify_chain(chain, "Settings"),
            Decision.UNKNOWN,
        )


if __name__ == "__main__":
    unittest.main()
