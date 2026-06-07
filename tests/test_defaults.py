"""PATCH 9 — verifica che i default di bot.py siano allineati con .env.example."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest.mock as _mock
with _mock.patch.dict(os.environ, {"PAPER_MODE": "true"}, clear=False):
    import importlib


class TestBotDefaults(unittest.TestCase):

    def _import_bot_no_env(self):
        """Importa bot senza .env e senza variabili ambiente legate ai parametri.
        Mock load_dotenv() per impedire la lettura del file .env locale.
        """
        clean_env = {
            "PAPER_MODE": "true",
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        }
        with _mock.patch("dotenv.load_dotenv", return_value=None):
            with _mock.patch.dict(os.environ, clean_env, clear=True):
                if "bot" in sys.modules:
                    del sys.modules["bot"]
                import bot as _bot
                return _bot

    def test_ema_slow_default_is_21(self):
        _bot = self._import_bot_no_env()
        self.assertEqual(_bot.CONFIG["ema_slow"], 21,
                         f"ema_slow default atteso 21, trovato {_bot.CONFIG['ema_slow']}")

    def test_bb_dev_default_is_2_0(self):
        _bot = self._import_bot_no_env()
        self.assertAlmostEqual(_bot.CONFIG["bb_dev"], 2.0, places=6,
                               msg=f"bb_dev default atteso 2.0, trovato {_bot.CONFIG['bb_dev']}")

    def test_allow_short_default_is_false(self):
        _bot = self._import_bot_no_env()
        self.assertFalse(_bot.CONFIG["allow_short"],
                         f"allow_short default atteso False, trovato {_bot.CONFIG['allow_short']}")

    def test_defaults_align_with_validation_config(self):
        """I default critici di bot.py devono corrispondere a VALIDATION_CONFIG."""
        _bot = self._import_bot_no_env()
        from validate_backtest import VALIDATION_CONFIG
        critical_keys = ("ema_slow", "bb_dev", "allow_short", "fee_pct", "slippage_pct")
        for k in critical_keys:
            self.assertEqual(_bot.CONFIG[k], VALIDATION_CONFIG[k],
                             f"bot.CONFIG['{k}'] ({_bot.CONFIG[k]}) != VALIDATION_CONFIG['{k}'] ({VALIDATION_CONFIG[k]})")

    def test_bot_import_without_env_does_not_raise(self):
        """Importare bot senza .env non solleva eccezioni."""
        try:
            self._import_bot_no_env()
        except SystemExit:
            pass  # PAPER_MODE=false lancerebbe SystemExit, qui e' true
        except Exception as e:
            self.fail(f"Import bot senza .env ha sollevato: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
