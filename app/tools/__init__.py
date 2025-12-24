from .fetch_code_file import fetch_code_file

# Tool registry for dynamic lookup
TOOL_REGISTRY = {
    "fetch_code_file": fetch_code_file,
}