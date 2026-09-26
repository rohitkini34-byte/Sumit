import json
from pathlib import Path

import pytest

from app.config import Settings
from app.i18n import LANGS, detect_language_rule_based, t
from app.whatsapp.templates import TEMPLATES, button_labels, meta_template_payload, render


def _prod(**kw):
    base = dict(APP_ENV="prod", WA_MODE="live", CALENDAR_MODE="google", LLM_MODE="live", DEV_ALLOWED_NUMBERS="",
                FERNET_KEY="x" * 44, SESSION_SECRET="prod-secret", PHONE_HASH_PEPPER="prod-pepper")
    base.update(kw)
    return Settings(**base)


def test_prod_ok():
    _prod()


@pytest.mark.parametrize("bad", [dict(WA_MODE="mock"), dict(CALENDAR_MODE="fake"), dict(LLM_MODE="mock"),
                                 dict(DEV_ALLOWED_NUMBERS="+911234567890")])
def test_prod_refuses_dev_modes(bad):
    with pytest.raises(ValueError):
        _prod(**bad)


def test_prod_refuses_default_secrets():
    with pytest.raises(ValueError):
        _prod(SESSION_SECRET=Settings.model_fields["SESSION_SECRET"].default)


def test_i18n_key_parity():
    d = Path(__file__).resolve().parents[1] / "app" / "i18n"
    keys = [set(json.loads((d / f"{l}.json").read_text(encoding="utf-8"))) for l in LANGS]
    assert keys[0] == keys[1] == keys[2]


def test_button_and_label_limits():
    for lang in LANGS:
        for k in ("consent.agree", "consent.decline", "menu.book", "menu.my", "menu.info", "book.confirm_btn",
                  "book.change_btn", "cancel.yes", "cancel.keep", "resched.confirm_btn", "slots.list_button",
                  "late.ask_button"):
            assert len(t(k, lang)) <= 20, (lang, k, t(k, lang))
        for name in TEMPLATES:
            for label in button_labels(name, lang):
                assert len(label) <= 20, (lang, name, label)
        for m in (10, 20, 30, 45):
            assert len(t(f"late.min{m}", lang)) <= 24


def test_templates_render_all_languages():
    for name, d in TEMPLATES.items():
        for lang in LANGS:
            params = [f"v{i}" for i in range(len(d.variables))]
            out = render(name, lang, params)
            assert "{{" not in out and all(p in out for p in params)
            payload = meta_template_payload("+911", name, lang, params, [f"b{i}" for i in range(d.buttons)])
            assert payload["template"]["name"] == name and payload["template"]["language"]["code"] == lang


def test_language_detection():
    assert detect_language_rule_based("Kal subah appointment chahiye") == "en"
    assert detect_language_rule_based("उद्या संध्याकाळी वेळ मिळेल का?") == "mr"
    assert detect_language_rule_based("मुझे कल शाम को अपॉइंटमेंट चाहिए") == "hi"
