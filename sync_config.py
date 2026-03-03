#!/usr/bin/env python3

import json
import os
import sys
from datetime import datetime

DEFAULT_CONFIG_PATH = "/home/node/.openclaw/openclaw.json"
DEFAULT_WORKSPACE_PATH = "/home/node/.openclaw/workspace"
QMD_COMMAND_PATH = "/usr/local/bin/qmd"


def ensure_path(data, keys):
    current = data
    for key in keys:
        if key not in current:
            current[key] = {}
        current = current[key]
    return current


def utc_timestamp_ms():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def split_csv_values(raw):
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def first_csv_value(raw, fallback):
    values = split_csv_values(raw)
    return values[0] if values else fallback


def with_provider_prefix(model_id, default_provider="default"):
    return f"{default_provider}/{model_id}"


def upsert_provider_models(config, provider_name, api_key, base_url, protocol, model_ids_raw, context_window, max_tokens):
    if not ((api_key and base_url) or model_ids_raw):
        return False

    provider = ensure_path(config, ["models", "providers", provider_name])
    if api_key:
        provider["apiKey"] = api_key
    if base_url:
        provider["baseUrl"] = base_url
    provider["api"] = protocol or "openai-completions"

    models = provider.get("models", [])
    model_ids = split_csv_values(model_ids_raw)

    for model_id in model_ids:
        model_entry = next((item for item in models if item.get("id") == model_id), None)

        if not model_entry:
            model_entry = {
                "id": model_id,
                "name": model_id,
                "reasoning": False,
                "input": ["text", "image"],
                "cost": {
                    "input": 0,
                    "output": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                },
            }
            models.append(model_entry)

        model_entry["contextWindow"] = int(context_window or 200000)
        model_entry["maxTokens"] = int(max_tokens or 8192)

    provider["models"] = models
    return True


def migrate_legacy_feishu_config(config):
    feishu_config = config.get("channels", {}).get("feishu", {})
    if "appId" in feishu_config and "accounts" not in feishu_config:
        print("检测到飞书旧版本格式，执行迁移...")
        old_app_id = feishu_config.pop("appId", "")
        old_app_secret = feishu_config.pop("appSecret", "")
        old_bot_name = feishu_config.pop("botName", "OpenClaw Bot")
        feishu_config["accounts"] = {
            "main": {
                "appId": old_app_id,
                "appSecret": old_app_secret,
                "botName": old_bot_name,
            }
        }


def sync_model_settings(config, env):
    if env.get("SYNC_MODEL_CONFIG", "true").lower() != "true":
        return

    primary_active = upsert_provider_models(
        config,
        "default",
        env.get("API_KEY"),
        env.get("BASE_URL"),
        env.get("API_PROTOCOL"),
        env.get("MODEL_ID") or "gpt-4o",
        env.get("CONTEXT_WINDOW"),
        env.get("MAX_TOKENS"),
    )

    secondary_provider_name = env.get("MODEL2_NAME") or "model2"
    secondary_active = upsert_provider_models(
        config,
        secondary_provider_name,
        env.get("MODEL2_API_KEY"),
        env.get("MODEL2_BASE_URL"),
        env.get("MODEL2_PROTOCOL"),
        env.get("MODEL2_MODEL_ID") or "",
        env.get("MODEL2_CONTEXT_WINDOW"),
        env.get("MODEL2_MAX_TOKENS"),
    )

    primary_model_id = first_csv_value(env.get("MODEL_ID"), "gpt-4o")
    image_model_id = first_csv_value(env.get("IMAGE_MODEL_ID"), primary_model_id)

    if primary_active:
        ensure_path(config, ["agents", "defaults", "model"])["primary"] = with_provider_prefix(primary_model_id)
        ensure_path(config, ["agents", "defaults", "imageModel"])["primary"] = with_provider_prefix(image_model_id)

    defaults = ensure_path(config, ["agents", "defaults"])
    defaults["workspace"] = env.get("WORKSPACE") or DEFAULT_WORKSPACE_PATH

    memory_qmd = config.get("memory", {}).get("qmd")
    if memory_qmd:
        memory_qmd["command"] = QMD_COMMAND_PATH
        for path_item in memory_qmd.get("paths", []):
            if path_item.get("name") == "workspace":
                path_item["path"] = defaults["workspace"]

    message = f"✅ 模型同步完成: 主模型={with_provider_prefix(primary_model_id)}"
    if image_model_id != primary_model_id:
        message += f", 图片模型={with_provider_prefix(image_model_id)}"
    if secondary_active:
        message += f", 已启用备用提供商: {secondary_provider_name}"
    print(message)


def sync_telegram(channel_config, env):
    channel_config.update(
        {
            "botToken": env["TELEGRAM_BOT_TOKEN"],
            "dmPolicy": "pairing",
            "groupPolicy": "allowlist",
            "streamMode": "partial",
        }
    )


def sync_feishu(channel_config, env):
    channel_config.update({"enabled": True, "dmPolicy": "pairing", "groupPolicy": "open"})
    main = ensure_path(channel_config, ["accounts", "main"])
    main.update(
        {
            "appId": env["FEISHU_APP_ID"],
            "appSecret": env["FEISHU_APP_SECRET"],
            "botName": env.get("FEISHU_BOT_NAME") or "OpenClaw Bot",
        }
    )
    if env.get("FEISHU_DOMAIN"):
        main["domain"] = env["FEISHU_DOMAIN"]


def sync_dingtalk(channel_config, env):
    channel_config.update(
        {
            "enabled": True,
            "clientId": env["DINGTALK_CLIENT_ID"],
            "clientSecret": env["DINGTALK_CLIENT_SECRET"],
            "robotCode": env.get("DINGTALK_ROBOT_CODE") or env["DINGTALK_CLIENT_ID"],
            "dmPolicy": "open",
            "groupPolicy": "open",
            "messageType": "markdown",
            "allowFrom": ["*"],
        }
    )
    if env.get("DINGTALK_CORP_ID"):
        channel_config["corpId"] = env["DINGTALK_CORP_ID"]
    if env.get("DINGTALK_AGENT_ID"):
        channel_config["agentId"] = env["DINGTALK_AGENT_ID"]


def sync_qqbot(channel_config, env):
    channel_config.update(
        {
            "enabled": True,
            "appId": env["QQBOT_APP_ID"],
            "clientSecret": env["QQBOT_CLIENT_SECRET"],
        }
    )


def sync_wecom(channel_config, env):
    channel_config.update(
        {
            "enabled": True,
            "token": env["WECOM_TOKEN"],
            "encodingAesKey": env["WECOM_ENCODING_AES_KEY"],
        }
    )
    if "commands" not in channel_config:
        channel_config["commands"] = {
            "enabled": True,
            "allowlist": ["/new", "/status", "/help", "/compact"],
        }


def sync_channels_and_plugins(config, env):
    channels = ensure_path(config, ["channels"])
    plugins = ensure_path(config, ["plugins"])
    entries = ensure_path(plugins, ["entries"])
    installs = ensure_path(plugins, ["installs"])

    if env.get("OPENCLAW_PLUGINS_ENABLED"):
        plugins["enabled"] = env["OPENCLAW_PLUGINS_ENABLED"].lower() == "true"

    sync_rules = [
        {
            "required": ["TELEGRAM_BOT_TOKEN"],
            "channel_id": "telegram",
            "sync_fn": sync_telegram,
            "install": None,
        },
        {
            "required": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
            "channel_id": "feishu",
            "sync_fn": sync_feishu,
            "install": {
                "source": "npm",
                "spec": "@openclaw/feishu",
                "installPath": "/home/node/.openclaw/extensions/feishu",
            },
        },
        {
            "required": ["DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"],
            "channel_id": "dingtalk",
            "sync_fn": sync_dingtalk,
            "install": {
                "source": "npm",
                "spec": "https://github.com/soimy/clawdbot-channel-dingtalk.git",
                "installPath": "/home/node/.openclaw/extensions/dingtalk",
            },
        },
        {
            "required": ["QQBOT_APP_ID", "QQBOT_CLIENT_SECRET"],
            "channel_id": "qqbot",
            "sync_fn": sync_qqbot,
            "install": {
                "source": "path",
                "sourcePath": "/home/node/.openclaw/qqbot",
                "installPath": "/home/node/.openclaw/extensions/qqbot",
            },
        },
        {
            "required": ["WECOM_TOKEN", "WECOM_ENCODING_AES_KEY"],
            "channel_id": "wecom",
            "sync_fn": sync_wecom,
            "install": {
                "source": "npm",
                "spec": "@sunnoy/wecom",
                "installPath": "/home/node/.openclaw/extensions/wecom",
            },
        },
    ]

    for rule in sync_rules:
        channel_id = rule["channel_id"]
        has_required_env = all(env.get(key) for key in rule["required"])

        if has_required_env:
            channel_config = ensure_path(channels, [channel_id])
            rule["sync_fn"](channel_config, env)
            entries[channel_id] = {"enabled": True}

            install_info = rule["install"]
            if install_info and channel_id not in installs:
                install_record = dict(install_info)
                install_record["installedAt"] = utc_timestamp_ms()
                installs[channel_id] = install_record

            print(f"✅ 渠道同步: {channel_id}")
            continue

        if channel_id in entries and entries[channel_id].get("enabled"):
            entries[channel_id]["enabled"] = False
            print(f"🚫 环境变量缺失，已禁用渠道: {channel_id}")

    plugins["allow"] = [plugin_name for plugin_name, state in entries.items() if state.get("enabled")]
    print("📦 已配置插件集合: " + ", ".join(plugins["allow"]))


def sync_gateway(config, env):
    if not env.get("OPENCLAW_GATEWAY_TOKEN"):
        return

    gateway = ensure_path(config, ["gateway"])
    gateway["port"] = int(env.get("OPENCLAW_GATEWAY_PORT") or 18789)
    gateway["bind"] = env.get("OPENCLAW_GATEWAY_BIND") or "0.0.0.0"
    gateway["mode"] = env.get("OPENCLAW_GATEWAY_MODE") or "local"

    control_ui = ensure_path(gateway, ["controlUi"])
    control_ui["allowInsecureAuth"] = env.get("OPENCLAW_GATEWAY_ALLOW_INSECURE_AUTH", "true").lower() == "true"
    control_ui["dangerouslyDisableDeviceAuth"] = (
        env.get("OPENCLAW_GATEWAY_DANGEROUSLY_DISABLE_DEVICE_AUTH", "false").lower() == "true"
    )
    if env.get("OPENCLAW_GATEWAY_ALLOWED_ORIGINS"):
        control_ui["allowedOrigins"] = split_csv_values(env.get("OPENCLAW_GATEWAY_ALLOWED_ORIGINS"))

    auth = ensure_path(gateway, ["auth"])
    auth["token"] = env["OPENCLAW_GATEWAY_TOKEN"]
    auth["mode"] = env.get("OPENCLAW_GATEWAY_AUTH_MODE") or "token"
    print("✅ Gateway 同步完成")


def sync_config(config_path):
    with open(config_path, "r", encoding="utf-8") as file:
        config = json.load(file)

    env = os.environ
    migrate_legacy_feishu_config(config)
    sync_model_settings(config, env)
    sync_channels_and_plugins(config, env)
    sync_gateway(config, env)

    ensure_path(config, ["meta"])["lastTouchedAt"] = utc_timestamp_ms()
    with open(config_path, "w", encoding="utf-8") as file:
        json.dump(config, file, indent=2, ensure_ascii=False)


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    try:
        sync_config(config_path)
    except Exception as error:
        print(f"❌ 同步失败: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()