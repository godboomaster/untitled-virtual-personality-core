# Маппинг пользователей мессенджера, заполняется динамически при получении сообщений.

_known_users: dict[str, dict] = {}


def register_user(user_id: str | int, name: str, username: str | None = None):
    # Регистрирует пользователя при первом взаимодействии.
    uid = str(user_id)
    if uid not in _known_users:
        _known_users[uid] = {"name": name, "username": username or ""}


def get_user_display(user_id: str) -> str:
    # Отображаемое имя пользователя по ID.
    user = _known_users.get(str(user_id))
    if user:
        return user["name"]
    return f"User_{user_id}"


def get_user_tag(user_id: str) -> str:
    # Тег для сообщений в формате "Имя (ID)".
    user = _known_users.get(str(user_id))
    if user:
        return f"{user['name']} ({user_id})"
    return f"User_{user_id}"


def get_username(user_id: str) -> str:
    # Юзернейм пользователя (без @) или пустая строка, если пользователь неизвестен.
    user = _known_users.get(str(user_id))
    return user["username"] if user else ""


def is_known_user(user_id: str) -> bool:
    return str(user_id) in _known_users
