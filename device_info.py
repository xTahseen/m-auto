import random

ANDROID_DEVICES = [
    {
        "brand": "ONEPLUS",
        "model": "CPH2691",
        "device": "OP5D3BL1",
        "product": "CPH2691IN",
        "display": "CPH2691_16.0.2.400(EX01)",
        "os": "Android v16",
    },
    {
        "brand": "SAMSUNG",
        "model": "SM-S928B",
        "device": "b0q",
        "product": "SM-S928BZKGXFE",
        "display": "S928BXXS3AXL1",
        "os": "Android v14",
    },
    {
        "brand": "XIAOMI",
        "model": "23127PN0CC",
        "device": "aristotle",
        "product": "aristotle_global",
        "display": "23127PN0CC_V816.0.5.0.UMCMIXM",
        "os": "Android v13",
    },
    {
        "brand": "GOOGLE",
        "model": "Pixel 8 Pro",
        "device": "husky",
        "product": "husky",
        "display": "AP2A.240805.005",
        "os": "Android v14",
    },
    {
        "brand": "SAMSUNG",
        "model": "SM-A546E",
        "device": "a54x",
        "product": "SM-A546EZKDINS",
        "display": "A546EXXS5CXL1",
        "os": "Android v14",
    },
]

REGIONS = ["US", "KR", "RU", "BR", "IN", "GB", "DE"]
GMT_OFFSETS = ["-0700", "+0900", "+0300", "+0530", "+0000", "+0800", "+0500"]
APP_VERSION = "7.0.7"


def random_hex(length=16):
    return "".join(random.choices("0123456789abcdef", k=length))


def random_device_info():
    d = random.choice(ANDROID_DEVICES)
    region = random.choice(REGIONS)
    gmt_offset = random.choice(GMT_OFFSETS)
    device_unique_id = random_hex(16)
    push_token = f"c3m9EnQkQEK5fQ4NrMvWDW:APA91b{random_hex(134)}"
    return {
        "os": d["os"],
        "platform": "android",
        "device": f"BRAND: {d['brand']}, MODEL: {d['model']}, DEVICE: {d['device']}, PRODUCT: {d['product']}, DISPLAY: {d['display']}",
        "pushToken": push_token,
        "deviceUniqueId": device_unique_id,
        "deviceLanguage": "en",
        "deviceRegion": region,
        "simRegion": "",
        "deviceGmtOffset": gmt_offset,
        "deviceRooted": 0,
        "deviceEmulator": 0,
        "appVersion": APP_VERSION,
    }
