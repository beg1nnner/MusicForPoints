"""Шаблоны сообщений для чата Twitch."""


def render(template: str, **values) -> str:
    """Подставляет {имена} в шаблон. Чат не принимает переносы строк и сообщения длиннее 500 символов."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    text = " ".join(template.split())[:450]
    if text.startswith(("/", ".")):  # не даём названию трека превратиться в команду чата
        text = "▸ " + text
    return text
