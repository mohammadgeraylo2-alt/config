from rulog import Client

client = Client()

phone = input("شماره روبیکا: ").strip()

# ارسال کد
response = client.sendCode(
    "android",
    phone,
    send_type=True
)

phone_code_hash = response["data"]["phone_code_hash"]

print("کد تأیید به روبیکا/شماره ارسال شد.")

code = input("کد تأیید: ").strip()

# ورود
result = cl

print("Login result:")
print(result)
