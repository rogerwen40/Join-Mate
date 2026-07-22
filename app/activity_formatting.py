from app.models import Activity


KEYWORD_EMOJIS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("羽球", "badminton"), "🏸"),
    (("桌球", "乒乓", "table tennis", "ping pong"), "🏓"),
    (("籃球", "basketball"), "🏀"),
    (("排球", "volleyball"), "🏐"),
    (("足球", "soccer", "football"), "⚽"),
    (("棒球", "壘球", "baseball", "softball"), "⚾"),
    (("匹克球", "pickleball"), "🎾"),
    (("網球", "tennis"), "🎾"),
    (("保齡球", "保齡", "bowling"), "🎳"),
    (("撞球", "billiards", "billiard", "pool table"), "🎱"),
    (("高爾夫", "golf"), "⛳"),
    (("游泳", "swimming", "swim"), "🏊"),
    (("跑步", "路跑", "慢跑", "running", "jogging"), "🏃"),
    (("單車", "自行車", "腳踏車", "cycling", "bicycle"), "🚴"),
    (("健身", "重訓", "gym", "workout"), "🏋️"),
    (("爬山", "登山", "健行", "hiking"), "🥾"),
)

TYPE_EMOJIS = {
    "吃飯": "🍽️",
    "運動": "🏅",
    "桌遊": "🎲",
    "討論": "💬",
    "其他": "🎉",
}


def detect_activity_emoji(activity: Activity) -> str:
    searchable_text = f"{activity.title} {activity.description}".lower()
    for keywords, emoji in KEYWORD_EMOJIS:
        if any(keyword in searchable_text for keyword in keywords):
            return emoji
    return TYPE_EMOJIS.get(activity.activity_type, "🎉")
