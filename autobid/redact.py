"""
Маскирование секретов в записях рекордеров: initData, cookies, токены, пароли, ключи API,
а в текстах экранов ещё и номера карт и телефонов.

Записи отправляют разработчику, поэтому в них не должно остаться ничего, что даёт доступ к аккаунту.
Правила намеренно перестраховываются: лучше скрыть лишнее, чем пропустить токен.
"""
import json
import re
from urllib.parse import unquote_plus, urlsplit

MASK = "***"

# Ключи сравниваются без регистра и без символов, кроме букв и цифр: "X-Api-Key" → "xapikey".
STRONG_KEYS = {
    "initdata", "tgwebappdata", "tginitdata", "webappdata", "rawinitdata",
    "authorization", "proxyauthorization", "cookie", "setcookie",
    "password", "passwd", "passphrase", "pass", "pwd", "secret", "clientsecret", "privatekey",
    "mnemonic", "seed", "seedphrase", "otp", "totp", "pin", "pincode", "cvv", "cvc",
    "apikey", "xapikey", "accesstoken", "refreshtoken", "idtoken", "authtoken", "bearertoken",
    "sessiontoken", "sessionid", "session", "sid", "csrftoken", "xsrftoken", "xcsrftoken", "xxsrftoken",
    "xauthtoken", "apitoken", "bottoken", "cryptopayapitoken", "rocketpaykey",
    "hash", "signature", "queryid",
}
# Однозначные слова в любом месте ключа ("refresh_token_v2", "user_secret", "X-Init-Data") и неоднозначные
# окончания ("base_token" бывает и монетой) — скрываем, если значение похоже на секрет.
SECRET_PARTS = ("accesstoken", "refreshtoken", "idtoken", "authtoken", "apitoken", "apikey", "bearer", "secret",
                "password", "passwd", "privatekey", "initdata", "webappdata", "sessionid", "sessiontoken",
                "mnemonic", "seedphrase", "cookie", "csrf", "xsrf")
SECRET_SUFFIXES = ("token", "signature", "authorization", "hash")
WEAK_KEYS = {"key", "auth", "jwt", "ticket", "sign", "sig"}
# Реквизиты: в значениях оставляем только последние 4 цифры.
DIGIT_KEYS = {"card", "cardnumber", "cardno", "pan", "iban", "accountnumber", "bankaccount", "phone",
              "phonenumber", "msisdn", "requisite", "requisites", "paymentdetails"}

_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic|tma|token)\s+[A-Za-z0-9._~+/=\-%:&]{8,}")
_BOT_TOKEN = re.compile(r"(?<!\d)\d{1,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])")   # и в пути /bot<токен>/; app_id бывает коротким
_CHEQUE = re.compile(r"(?<![A-Za-z0-9_])(CQ|mc_)[A-Za-z0-9_-]{6,}")              # чеки CryptoBot/xRocket: кто знает код — забирает деньги
_PARAM = re.compile(r"(?i)\b((?:tgWebAppData|initData|init_data|tg_init_data|webAppData)=)[^&\s\"'#<>]+")
_SPAN = re.compile(r"[^\s\"'<>]+")
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+")
_REL_URL = re.compile(r"^(?:[a-z][a-z0-9+.\-]*:)?[^\s?#]*[?#]\S*=\S*$", re.I)    # /путь?token=… или tg://…?startapp=…
# Пара «ключ: значение» в обычном тексте (не JSON). Квантификатор ограничен ({0,40}?), иначе на длинных
# base64/hex строках regex уходит в квадратичное время и морозит рекордер. Значение может начинаться со схемы.
_KV_TEXT = re.compile(
    r"(?i)([\"']?[a-z0-9_\-]{0,40}?(?:token|secret|password|passwd|api[_\-]?key|init[_\-]?data|session[_\-]?id|"
    r"cookie|authorization|signature|токен|ключ)[\"']?\s*[:=]\s*[\"']?)"
    r"((?:(?:bearer|basic|tma|token)\s+)?[^\"'&\s,;{}]{6,})")
_CARD_FMT = re.compile(r"(?<!\d)\d{4}([ \-])\d{4}\1\d{4}\1\d{4}(?:\1\d{1,3})?(?!\d)")
_CARD_RAW = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_PHONE = re.compile(r"(?<![\w+])\+\d[\d \-()]{8,18}\d(?!\w)")


def norm_key(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _luhn(digits):
    total, alt = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
        alt = not alt
    return total % 10 == 0


def _mask_card_fmt(m):
    digits = re.sub(r"\D", "", m.group(0))
    return "**** **** **** " + digits[-4:]


def _mask_card_raw(m):
    d = m.group(0)
    return "*" * (len(d) - 4) + d[-4:] if _luhn(d) else d


def _mask_phone(m):
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) < 10:
        return m.group(0)
    return "+" + digits[:2] + "*" * (len(digits) - 4) + digits[-2:]


def _mask_digits(value):
    """Реквизит: '2202 2024 1234 5678' → '*** 5678', '+79161234567' → '*** 4567'."""
    digits = re.sub(r"\D", "", value)
    return MASK + " " + digits[-4:] if len(digits) >= 6 else value


def _looks_secret(value, min_len=16):
    return isinstance(value, str) and len(value) >= min_len and not re.search(r"\s", value)


def _initdata_span(m):
    s = m.group(0)
    low = s.lower()
    if "auth_date" in low and ("hash" in low or "signature" in low):
        return "***initData***"
    return s


def key_rule(key):
    """'strong' — скрыть всегда, 'suffix'/'weak' — если похоже на секрет, 'digits' — реквизиты, None — не трогать."""
    k = norm_key(key)
    if k in STRONG_KEYS:
        return "strong"
    if k in DIGIT_KEYS:
        return "digits"
    if k.endswith(SECRET_SUFFIXES) or any(p in k for p in SECRET_PARTS):
        return "suffix"
    if k in WEAK_KEYS:
        return "weak"
    return None


def redact_url(url):
    """Скрыть секреты в адресе: параметры запроса и фрагмента (#tgWebAppData=…), логин:пароль@, токен бота в пути."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return _text_patterns(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = MASK + "@" + netloc.rsplit("@", 1)[1]
    path = _BOT_TOKEN.sub("***bot_token***", parts.path)
    out = f"{parts.scheme}://{netloc}{path}" if parts.scheme else path
    if parts.query:
        out += "?" + redact_qs(parts.query)
    if parts.fragment:
        out += "#" + (redact_qs(parts.fragment) if "=" in parts.fragment else parts.fragment)
    return out


def redact_qs(qs):
    """Строка вида a=1&b=2 без перекодирования значений (чтобы адрес остался узнаваемым)."""
    out = []
    for pair in qs.split("&"):
        k, sep, v = pair.partition("=")
        nk = norm_key(unquote_plus(k))
        rule = key_rule(nk)
        if nk == "tgwebappthemeparams" and v:
            v = "…"
        elif rule == "strong" and v:
            v = MASK
        elif rule in ("suffix", "weak") and _looks_secret(unquote_plus(v), 12 if rule == "suffix" else 16):
            v = MASK
        elif rule == "digits" and v:
            v = _mask_digits(unquote_plus(v))
        else:
            v = _text_patterns(v)
        out.append(k + sep + v)
    return "&".join(out)


def _text_patterns(s):
    s = _JWT.sub("***jwt***", s)
    s = _AUTH_SCHEME.sub(lambda m: m.group(1) + " " + MASK, s)
    s = _BOT_TOKEN.sub("***bot_token***", s)
    s = _CHEQUE.sub(lambda m: m.group(1) + MASK, s)
    s = _PARAM.sub(lambda m: m.group(1) + MASK, s)
    s = _SPAN.sub(_initdata_span, s)
    s = _CARD_FMT.sub(_mask_card_fmt, s)
    return s


def redact_str(s, aggressive=False):
    """Строка любого вида. aggressive=True — тексты экранов и полей ввода: ещё карты без пробелов и телефоны."""
    if not isinstance(s, str) or not s:
        return s
    if re.match(r"^https?://\S+$", s):
        s = redact_url(s)
    elif _REL_URL.match(s):
        s = redact_url(s)           # относительная ссылка/редирект с секретом: /withdraw?token=…, #access_token=…
    else:
        s = _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), s)
    s = _text_patterns(s)
    if aggressive:
        s = _CARD_RAW.sub(_mask_card_raw, s)
        s = _PHONE.sub(_mask_phone, s)
    return s


def redact_text_kv(s):
    """Текст, который не разобрался как JSON (обрезанный ответ, SSE, form-data): ещё и пары ключ=значение."""
    if not isinstance(s, str) or not s:
        return s
    # Сначала redact_str (JWT, схемы Bearer/tma, токен бота, ссылки), потом пары ключ=значение по тому,
    # что осталось. Обратный порядок съедал слово «Bearer» как значение и оставлял сам токен.
    return _KV_TEXT.sub(lambda m: m.group(1) + MASK, redact_str(s))


def redact_value(key, value, aggressive=False):
    rule = key_rule(key) if key is not None else None
    if isinstance(value, (dict, list)):
        return redact_obj(value, aggressive=aggressive)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str) and MASK in value and rule in ("strong", "digits"):
        return value   # уже скрыто (например, "Bearer ***" или "session=***; Path=/") — второй проход не портит
    if rule == "strong":
        return MASK if value != "" else value
    if rule == "digits":
        return _mask_digits(str(value))
    if rule == "suffix" and (_looks_secret(value, 12) or isinstance(value, (int, float))):
        return MASK
    if rule == "weak" and _looks_secret(value):
        return MASK
    if isinstance(value, str):
        return redact_str(value, aggressive)
    return value


def redact_obj(obj, aggressive=False):
    """JSON-подобная структура: правила по именам ключей + шаблоны в строках."""
    if isinstance(obj, dict):
        return {k: redact_value(k, v, aggressive) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(x, aggressive) for x in obj]
    if isinstance(obj, str):
        return redact_str(obj, aggressive)
    return obj


def redact_headers(headers, keep=None):
    """Заголовки HTTP. keep — множество имён, которые оставить (остальные выбросить); None — оставить все."""
    out = {}
    for name, value in (headers or {}).items():
        low = name.lower()
        if keep is not None and low not in keep and not low.startswith(("x-ratelimit", "ratelimit")):
            continue
        k = norm_key(name)
        if k in ("authorization", "proxyauthorization"):
            scheme = value.split(" ", 1)[0] if " " in value else ""
            out[low] = (scheme + " " + MASK).strip()
        elif k == "cookie":
            out[low] = "; ".join(c.split("=", 1)[0].strip() + "=" + MASK for c in value.split(";") if c.strip())
        elif k == "setcookie":
            lines = []
            for line in value.split("\n"):
                first, _, attrs = line.partition(";")
                lines.append(first.split("=", 1)[0].strip() + "=" + MASK + (";" + attrs if attrs else ""))
            out[low] = "\n".join(lines)
        elif key_rule(k) in ("strong", "suffix", "weak") or any(p in k for p in ("auth", "session", "access")):
            out[low] = MASK   # X-Auth / X-Session-Id / X-Access-… с непрозрачным значением
        else:
            out[low] = redact_str(value)
    return out


def redact_body(text, content_type=""):
    """Тело запроса или ответа. Возвращает (json_или_None, текст_или_None) — что-то одно."""
    if text is None or text == "":
        return None, text
    ct = (content_type or "").lower()
    stripped = text.lstrip()
    if "json" in ct or stripped[:1] in ("{", "["):
        try:
            return redact_obj(json.loads(text)), None
        except ValueError:
            pass
    if "x-www-form-urlencoded" in ct or re.fullmatch(r"[\w.\-%+]+=[^\s&]*(?:&[\w.\-%+]+=[^\s&]*)*", text.strip()):
        return None, redact_qs(text.strip())
    return None, redact_text_kv(text)
