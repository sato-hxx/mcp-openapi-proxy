"""
Utility functions for mcp-openapi-proxy.
"""

import os
import re
import sys
import json
import requests
import yaml
import hashlib
from typing import Dict, Optional, Tuple, List, Union
from mcp import types

# Import the configured logger
from .logging_setup import logger

# ツール名のプロトコル最大長を60文字に設定
PROTOCOL_MAX_LENGTH = 60

# 正規表現は変わらないが、PROTOCOL_MAX_LENGTH に合わせてリテラルを調整する必要がある場合がある
# 現状の正規表現は64まで許可しているので、このままでOK。
# 実際には、正規表現自体も`{1,}`のように柔軟にしておき、別途長さチェックを行うのが良いが、
# 現在の要件に合わせて、60文字を超える場合は後続の処理で対応する。
TOOL_NAME_REGEX_COMPILED = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

def setup_logging(debug: bool = False):
    """
    Configure logging for the application.
    """
    from .logging_setup import setup_logging as ls
    return ls(debug)

def normalize_tool_name(raw_name: str, max_length: Optional[int] = None) -> str:
    """
    Convert an HTTP method and path into a normalized tool name,
    applying character sanitization, length limits, and ensuring uniqueness with hashing.
    'max_length' can override the protocol limit for specific use cases.
    """
    try:
        if " " not in raw_name:
            logger.warning(f"Malformed raw tool name received: '{raw_name}'. Returning 'unknown_tool'.")
            return "unknown_tool"
        method, path = raw_name.split(" ", 1)

        # --- コロンのサニタイズ ---
        # パス内のコロンをアンダースコアに置換
        path = path.replace(':', '_')


        # Remove common uninformative url prefixes and leading/trailing slashes
        path = re.sub(r"/(api|rest|public)/?", "/", path).lstrip("/").rstrip("/")

        # Handle empty path
        if not path:
            path = "root"

        url_template_pattern = re.compile(r"\{([^}]+)\}")
        normalized_parts = []
        for part in path.split("/"):
            if url_template_pattern.search(part):
                params = url_template_pattern.findall(part)
                base = url_template_pattern.sub("", part)
                part = f"{base}_by_{'_'.join(p.lower() for p in params)}"

            part = part.replace(".", "_").replace("-", "_").replace("+", "_")
            if part:
                normalized_parts.append(part)

        base_tool_name = f"{method.lower()}_{'_'.join(normalized_parts)}"
        base_tool_name = re.sub(r"_+", "_", base_tool_name).strip("_")

        tool_name_prefix = os.getenv("TOOL_NAME_PREFIX", "")
        if tool_name_prefix:
            base_tool_name = f"{tool_name_prefix}{base_tool_name}"


        # --- 最終的な長さ制限を決定 ---
        # 引数で渡されたmax_length > 環境変数 > PROTOCOL_MAX_LENGTH の優先順位
        effective_max_length = PROTOCOL_MAX_LENGTH # デフォルトはプロトコル制限
        limit_source = "protocol"

        if max_length is not None:
            # 引数で渡された場合はそれを優先
            if max_length > 0:
                effective_max_length = max_length
                limit_source = f"argument ({max_length})"
            else:
                logger.warning(f"Invalid max_length argument: {max_length}. Ignoring.")
        else:
            # 引数がない場合、環境変数を確認
            max_length_env = os.getenv("TOOL_NAME_MAX_LENGTH")
            if max_length_env:
                try:
                    parsed_max_length = int(max_length_env)
                    if parsed_max_length > 0:
                        effective_max_length = parsed_max_length
                        limit_source = f"env var ({parsed_max_length})"
                    else:
                        logger.warning(f"Invalid TOOL_NAME_MAX_LENGTH env var: {max_length_env}. Ignoring.")
                except ValueError:
                    logger.warning(f"Invalid TOOL_NAME_MAX_LENGTH env var: {max_length_env}. Ignoring.")

        # ただし、最終的な有効な制限はPROTOCOL_MAX_LENGTHを超えることはない
        if effective_max_length > PROTOCOL_MAX_LENGTH:
            effective_max_length = PROTOCOL_MAX_LENGTH
            limit_source = f"{limit_source} (capped by protocol limit of {PROTOCOL_MAX_LENGTH})"

        # --- 長さ制限と重複回避のロジックを適用 ---
        final_tool_name = base_tool_name
        original_length = len(base_tool_name)

        if original_length > effective_max_length:
            logger.warning(
                f"Tool name '{base_tool_name}' ({original_length} chars) "
                f"exceeds {limit_source} limit of {effective_max_length} chars; attempting to generate unique shorter name."
            )

            # ユニーク性を高めるために、元のraw_nameからハッシュ値を末尾に追加する
            path_hash = hashlib.sha256(raw_name.encode('utf-8')).hexdigest()[:6]

            # ハッシュを付与しても effective_max_length を超えないように、残りの長さを計算
            remaining_length = effective_max_length - (len(path_hash) + 1) # +1はアンダースコア用

            if remaining_length > 0:
                final_tool_name = base_tool_name[:remaining_length] + '_' + path_hash
            else:
                final_tool_name = path_hash # 6文字なので制限は超えない

            logger.info(f"Final tool name (hashed): {final_tool_name}, length: {len(final_tool_name)}")
        else:
            # 長さ制限を超えない場合はそのまま使用
            logger.info(f"Final tool name: {final_tool_name}, length: {len(final_tool_name)}")


        # 最終的な名前が正規表現にマッチするか確認（念のため）
        # この正規表現は64文字まで許可しているので、effective_max_lengthが60でも問題ない
        if not TOOL_NAME_REGEX_COMPILED.match(final_tool_name):
            logger.error(f"Generated tool name '{final_tool_name}' (from '{raw_name}') still does not match regex '{TOOL_NAME_REGEX_COMPILED.pattern}'. Falling back to simple hash.")
            final_tool_name = hashlib.sha256(raw_name.encode('utf-8')).hexdigest()[:PROTOCOL_MAX_LENGTH]
            final_tool_name = final_tool_name.replace(':', '_').replace('.', '_') # 念のためコロンなどを除去

        return final_tool_name
    except Exception as e:
        logger.error(f"Error normalizing tool name '{raw_name}': {e}", exc_info=True)
        return "unknown_tool"

def fetch_openapi_spec(url: str, retries: int = 3) -> Optional[Dict]:
    """
    Fetch and parse an OpenAPI specification from a URL with retries.
    """
    logger.debug(f"Fetching OpenAPI spec from URL: {url}")
    attempt = 0
    while attempt < retries:
        try:
            if url.startswith("file://"):
                with open(url[7:], "r") as f:
                    content = f.read()
                spec_format = os.getenv("OPENAPI_SPEC_FORMAT", "json").lower()
                logger.debug(f"Using {spec_format.upper()} parser based on OPENAPI_SPEC_FORMAT env var")
                if spec_format == "yaml":
                    try:
                        spec = yaml.safe_load(content)
                        logger.debug(f"Parsed as YAML from {url}")
                    except yaml.YAMLError as ye:
                        logger.error(f"YAML parsing failed: {ye}. Raw content: {content[:500]}...")
                        return None
                else:
                    try:
                        spec = json.loads(content)
                        logger.debug(f"Parsed as JSON from {url}")
                    except json.JSONDecodeError as je:
                        logger.error(f"JSON parsing failed: {je}. Raw content: {content[:500]}...")
                        return None
            else:
                # Check IGNORE_SSL_SPEC env var
                ignore_ssl_spec = os.getenv("IGNORE_SSL_SPEC", "false").lower() in ("true", "1", "yes")
                verify_ssl_spec = not ignore_ssl_spec
                logger.debug(f"Fetching spec with SSL verification: {verify_ssl_spec} (IGNORE_SSL_SPEC={ignore_ssl_spec})")
                response = requests.get(url, timeout=10, verify=verify_ssl_spec)
                response.raise_for_status()
                content = response.text
                logger.debug(f"Fetched content length: {len(content)} bytes")
                try:
                    spec = json.loads(content)
                    logger.debug(f"Parsed as JSON from {url}")
                except json.JSONDecodeError:
                    try:
                        spec = yaml.safe_load(content)
                        logger.debug(f"Parsed as YAML from {url}")
                    except yaml.YAMLError as ye:
                        logger.error(f"YAML parsing failed: {ye}. Raw content: {content[:500]}...")
                        return None
            return spec
        except requests.RequestException as e:
            attempt += 1
            logger.warning(f"Fetch attempt {attempt}/{retries} failed: {e}")
            if attempt == retries:
                logger.error(f"Failed to fetch spec from {url} after {retries} attempts: {e}")
                return None
        except FileNotFoundError as e:
            logger.error(f"Failed to open local file spec {url}: {e}")
            return None
        except Exception as e:
            attempt += 1
            logger.warning(f"Unexpected error during fetch attempt {attempt}/{retries}: {e}")
            if attempt == retries:
                logger.error(f"Failed to process spec from {url} after {retries} attempts due to unexpected error: {e}")
                return None
    return None


def build_base_url(spec: Dict) -> Optional[str]:
    """
    Construct the base URL from the OpenAPI spec or override.
    """
    override = os.getenv("SERVER_URL_OVERRIDE")
    if override:
        urls = [url.strip() for url in override.split(",")]
        for url in urls:
            if url.startswith("http://") or url.startswith("https://"):
                logger.debug(f"SERVER_URL_OVERRIDE set, using first valid URL: {url}")
                return url
        logger.error(f"No valid URLs found in SERVER_URL_OVERRIDE: {override}")
        return None
    if "servers" in spec and spec["servers"]:
        # Ensure servers is a list and has items before accessing index 0
        if isinstance(spec["servers"], list) and len(spec["servers"]) > 0 and isinstance(spec["servers"][0], dict):
             server_url = spec["servers"][0].get("url")
             if server_url:
                 logger.debug(f"Using first server URL from spec: {server_url}")
                 return server_url
             else:
                 logger.warning("First server entry in spec missing 'url' key.")
        else:
             logger.warning("Spec 'servers' key is not a non-empty list of dictionaries.")

    # Fallback for OpenAPI v2 (Swagger)
    if "host" in spec and "schemes" in spec:
        scheme = spec["schemes"][0] if spec.get("schemes") else "https"
        base_path = spec.get("basePath", "")
        host = spec.get("host")
        if host:
            v2_url = f"{scheme}://{host}{base_path}"
            logger.debug(f"Using OpenAPI v2 host/schemes/basePath: {v2_url}")
            return v2_url
        else:
            logger.warning("OpenAPI v2 spec missing 'host'.")

    logger.error("Could not determine base URL from spec (servers/host/schemes) or SERVER_URL_OVERRIDE.")
    return None


def handle_auth(operation: Dict) -> Dict[str, str]:
    """
    Handle authentication based on environment variables and operation security.
    """
    headers = {}
    api_key = os.getenv("API_KEY")
    auth_type = os.getenv("API_AUTH_TYPE", "Bearer").lower()
    if api_key:
        if auth_type == "bearer":
            logger.debug(f"Using API_KEY as Bearer token.") # Avoid logging key prefix
            headers["Authorization"] = f"Bearer {api_key}"
        elif auth_type == "basic":
            logger.warning("API_AUTH_TYPE is Basic, but Basic Auth is not fully implemented yet.")
            # Potentially add basic auth implementation here if needed
        elif auth_type == "api-key":
            key_name = os.getenv("API_AUTH_HEADER", "Authorization")
            headers[key_name] = api_key
            logger.debug(f"Using API_KEY as API-Key in header '{key_name}'.") # Avoid logging key prefix
        else:
            logger.warning(f"Unsupported API_AUTH_TYPE: {auth_type}")
    # TODO: Add logic to check operation['security'] and spec['components']['securitySchemes']
    #       to potentially override or supplement env var based auth.
    return headers

def strip_parameters(parameters: Dict) -> Dict:
    """
    Strip specified parameters from the input based on STRIP_PARAM.
    """
    strip_param = os.getenv("STRIP_PARAM")
    if not strip_param or not isinstance(parameters, dict):
        return parameters
    logger.debug(f"Raw parameters before stripping '{strip_param}': {parameters}")
    result = parameters.copy()
    if strip_param in result:
        del result[strip_param]
        logger.debug(f"Stripped '{strip_param}'. Parameters after stripping: {result}")
    else:
        logger.debug(f"Parameter '{strip_param}' not found, no stripping performed.")
    return result

# Corrected function signature and implementation
def detect_response_type(response_text: str) -> Tuple[types.TextContent, str]:
    """
    Determine response type based on JSON validity. Always returns TextContent.
    """
    try:
        # Attempt to parse as JSON
        decoded_json = json.loads(response_text)

        # Check if it's already in MCP TextContent format (e.g., from another MCP component)
        if isinstance(decoded_json, dict) and decoded_json.get("type") == "text" and "text" in decoded_json:
             logger.debug("Response is already in TextContent format.")
             # Validate and return directly if possible, otherwise treat as nested JSON string
             try:
                 # Return the validated TextContent object
                 return types.TextContent(**decoded_json), "Passthrough TextContent response"
             except Exception:
                 logger.warning("Received TextContent-like structure, but failed validation. Stringifying.")
                 # Fall through to stringify the whole structure
                 pass

        # If parsing succeeded and it's not TextContent, return as TextContent with stringified JSON
        logger.debug("Response parsed as JSON, returning as stringified TextContent.")
        return types.TextContent(type="text", text=json.dumps(decoded_json)), "JSON response (stringified)"

    except json.JSONDecodeError:
        # If JSON parsing fails, treat as plain text
        logger.debug("Response is not valid JSON, treating as plain text.")
        return types.TextContent(type="text", text=response_text.strip()), "Non-JSON text response"
    except Exception as e:
        # Catch unexpected errors during detection
        logger.error(f"Error detecting response type: {e}", exc_info=True)
        return types.TextContent(type="text", text=f"Error detecting response type: {response_text[:100]}..."), "Error during response detection"


def get_additional_headers() -> Dict[str, str]:
    """
    Parse additional headers from EXTRA_HEADERS environment variable.
    """
    headers = {}
    extra_headers = os.getenv("EXTRA_HEADERS")
    if extra_headers:
        logger.debug(f"Parsing EXTRA_HEADERS: {extra_headers}")
        for line in extra_headers.splitlines():
            line = line.strip()
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                if key and value:
                    headers[key] = value
                    logger.debug(f"Added header from EXTRA_HEADERS: '{key}'")
                else:
                    logger.warning(f"Skipping invalid header line in EXTRA_HEADERS: '{line}'")
            elif line:
                 logger.warning(f"Skipping malformed line in EXTRA_HEADERS (no ':'): '{line}'")
    return headers

def is_tool_whitelist_set() -> bool:
    """
    Check if TOOL_WHITELIST environment variable is set and not empty.
    """
    return bool(os.getenv("TOOL_WHITELIST", "").strip())

def is_tool_whitelisted(endpoint: str) -> bool:
    """
    Check if an endpoint is allowed based on TOOL_WHITELIST.
    Allows all if TOOL_WHITELIST is not set or empty.
    Handles simple prefix matching and basic regex for path parameters.
    """
    whitelist_str = os.getenv("TOOL_WHITELIST", "").strip()
    # logger.debug(f"Checking whitelist - endpoint: '{endpoint}', TOOL_WHITELIST: '{whitelist_str}'") # Too verbose for every check

    if not whitelist_str:
        # logger.debug("No TOOL_WHITELIST set, allowing all endpoints.")
        return True

    whitelist_entries = [entry.strip() for entry in whitelist_str.split(",") if entry.strip()]

    # Normalize endpoint by removing leading/trailing slashes for comparison
    normalized_endpoint = "/" + endpoint.strip("/")

    for entry in whitelist_entries:
        normalized_entry = "/" + entry.strip("/")
        # logger.debug(f"Comparing '{normalized_endpoint}' against whitelist entry '{normalized_entry}'")

        if "{" in normalized_entry and "}" in normalized_entry:
            # Convert entry with placeholders like /users/{id}/posts to a regex pattern
            # Escape regex special characters, then replace placeholders
            pattern_str = re.escape(normalized_entry).replace(r"\{", "{").replace(r"\}", "}")
            pattern_str = re.sub(r"\{[^}]+\}", r"([^/]+)", pattern_str)
            # Ensure it matches the full path segment or the start of it
            pattern = "^" + pattern_str + "($|/.*)"
            try:
                if re.match(pattern, normalized_endpoint):
                    logger.debug(f"Endpoint '{normalized_endpoint}' matches whitelist pattern '{pattern}' from entry '{entry}'")
                    return True
            except re.error as e:
                 logger.error(f"Invalid regex pattern generated from whitelist entry '{entry}': {pattern}. Error: {e}")
                 continue # Skip this invalid pattern
        elif normalized_endpoint.startswith(normalized_entry):
             # Simple prefix match (e.g., /users allows /users/123)
             # Ensure it matches either the exact path or a path segment start
             if normalized_endpoint == normalized_entry or normalized_endpoint.startswith(normalized_entry + "/"):
                  logger.debug(f"Endpoint '{normalized_endpoint}' matches whitelist prefix '{normalized_entry}' from entry '{entry}'")
                  return True

    logger.debug(f"Endpoint '{normalized_endpoint}' not found in TOOL_WHITELIST.")
    return False
