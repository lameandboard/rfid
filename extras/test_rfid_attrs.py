"""Minimal self-check: verify Rfid.__init__ initialises config-derived attributes used in the scan path.

Run with:
    python3 extras/test_rfid_attrs.py

The test parses extras/rfid.py with the standard ``ast`` module (no Klipper runtime required)
and asserts that every ``self.<attr>`` that is assigned from a ``config.*`` call in
``Rfid.__init__`` is still present, and that a fixed set of known-critical scan-path
attributes (including those that caused real crashes, like ``auto_create_spool``) are
initialized.  This prevents regressions like the PR #7 omission of ``self.auto_create_spool``.
"""

import ast
import os
import unittest

_RFID_PY = os.path.join(os.path.dirname(__file__), "rfid.py")

# These attributes are read inside the hot scan path and MUST be initialised in
# __init__ via config.get*().  Any omission here causes an AttributeError at
# runtime (as happened with auto_create_spool in PR #7).
_REQUIRED_CONFIG_ATTRS = {
    "auto_create_spool",
    "auto_commit_on_scan",
    "auto_write",
    "scan_window",
    "scan_delay",
    "fast_mode",
    "candidate_ttl",
    "max_pages",
    "max_uids",
    "event_timeout",
    "spoolman_url",
    "uid_fast_scan",
}


def _collect_config_attrs_in_init(rfid_class: ast.ClassDef) -> set:
    """Return attrs assigned from a ``config.*`` call inside ``Rfid.__init__``."""
    attrs: set = set()
    for method in rfid_class.body:
        if not (isinstance(method, ast.FunctionDef) and method.name == "__init__"):
            continue
        for node in ast.walk(method):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if not (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        continue
                    val_src = ast.unparse(node.value)
                    if "config." in val_src:
                        attrs.add(target.attr)
        break
    return attrs


class TestRfidInitAttributes(unittest.TestCase):
    """Ensure Rfid.__init__ initialises every config-derived attribute used in the scan path."""

    @classmethod
    def setUpClass(cls):
        with open(_RFID_PY, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src, filename=_RFID_PY)

        cls.rfid_class = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "Rfid":
                cls.rfid_class = node
                break
        if cls.rfid_class is None:
            raise RuntimeError("Could not find class Rfid in extras/rfid.py")

        cls.init_config_attrs = _collect_config_attrs_in_init(cls.rfid_class)

    def test_auto_create_spool_initialized(self):
        """auto_create_spool must be assigned from config in __init__ (regression test for PR #7).

        PR #7 accidentally removed ``self.auto_create_spool = config.getboolean(...)``
        while keeping all code that reads ``self.auto_create_spool``, causing a runtime
        AttributeError crash whenever the scan timer fired.
        """
        self.assertIn(
            "auto_create_spool",
            self.init_config_attrs,
            "self.auto_create_spool must be assigned from config in Rfid.__init__. "
            "It was accidentally removed in PR #7.",
        )

    def test_all_required_scan_path_attrs_initialized(self):
        """Every attribute in _REQUIRED_CONFIG_ATTRS must be initialised from config in __init__."""
        missing = sorted(_REQUIRED_CONFIG_ATTRS - self.init_config_attrs)
        if missing:
            self.fail(
                "The following attributes are required in the scan path but are not"
                " assigned from config in Rfid.__init__:\n"
                + "".join(f"  self.{attr}\n" for attr in missing)
                + "\nAdd the missing config.get*() assignments to __init__."
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
