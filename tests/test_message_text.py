from ap import slack


def test_attachment_only_alert_renders_title_text_and_fields():
    m = {"text": "", "attachments": [{"fallback": "Warn: x", "title": "Warn: [schools][prd] Memoria alta", "text": "Uso: 80%",
                                       "fields": [{"title": "Tags", "value": "ecs:schools"}]}]}
    out = slack.message_text(m)
    assert "Memoria alta" in out and "Uso: 80%" in out and "Tags: ecs:schools" in out
    assert "Warn: x" not in out  # fallback only when nothing else rendered


def test_fallback_when_attachment_has_nothing_else():
    assert slack.message_text({"text": "", "attachments": [{"fallback": "solo esto"}]}) == "solo esto"


def test_block_only_bot_message():
    m = {"text": "", "blocks": [{"type": "header", "text": {"type": "plain_text", "text": "Alerta"}},
                                {"type": "section", "text": {"type": "mrkdwn", "text": "500 en /grades"}},
                                {"type": "context", "elements": [{"type": "mrkdwn", "text": "prd"}]}]}
    assert slack.message_text(m) == "Alerta\n500 en /grades\nprd"


def test_typed_message_is_not_duplicated_by_its_rich_text():
    m = {"text": "mirá <https://x.com|esto>", "blocks": [{"type": "rich_text", "elements": [
        {"type": "rich_text_section", "elements": [{"type": "text", "text": "mirá "},
                                                   {"type": "link", "url": "https://x.com", "text": "esto"}]}]}]}
    assert slack.message_text(m) == "mirá <https://x.com|esto>"


def test_rich_text_used_when_text_is_empty():
    m = {"text": "", "blocks": [{"type": "rich_text", "elements": [
        {"type": "rich_text_section", "elements": [{"type": "text", "text": "hola "}, {"type": "user", "user_id": "U1"}]}]}]}
    assert slack.message_text(m) == "hola <@U1>"
