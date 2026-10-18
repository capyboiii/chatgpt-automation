import unittest
from html.parser import HTMLParser
from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "static"


class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.append(attrs["id"])


class UiLayoutTests(unittest.TestCase):
    def test_each_mode_has_one_control_per_action(self):
        for filename, required in {
            "index.html": ("main-runbar", "run-btn", "btn-clear-jobs",
                           "btn-download-all", "results-panel"),
            "csvsession.html": ("btn-csv", "btn-img", "attach-panel",
                                "btn-start-csv", "btn-download-all", "results-panel"),
        }.items():
            parser = IdParser()
            parser.feed((STATIC / filename).read_text(encoding="utf-8"))
            for identifier in required:
                self.assertEqual(parser.ids.count(identifier), 1, (filename, identifier))
            self.assertEqual(len(parser.ids), len(set(parser.ids)), filename)

    def test_download_all_is_scoped_to_current_mode(self):
        mockup = (STATIC / "index.html").read_text(encoding="utf-8")
        csv = (STATIC / "csvsession.html").read_text(encoding="utf-8")
        self.assertIn('href="/api/jobs/zip?mode=mockup"', mockup)
        self.assertIn('href="/api/jobs/zip?mode=csv-session"', csv)


if __name__ == "__main__":
    unittest.main()
