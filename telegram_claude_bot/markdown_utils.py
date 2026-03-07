"""
Telegram markdown sanitization utilities.
Converts markdown to Telegram-compatible format (ParseMode.MARKDOWN).
"""

import re


def sanitize_markdown_for_telegram(text: str) -> str:
    """
    Convert markdown to Telegram-compatible ParseMode.MARKDOWN format.

    Telegram's MARKDOWN mode supports:
    - *bold* (single asterisk)
    - _italic_
    - `code`
    - ```code blocks```
    - [links](URL)

    Does NOT support:
    - Headers (##, ###)
    - **bold** (double asterisk - not standard)
    - Strikethrough
    - Nested formatting
    """
    if not text:
        return text

    # First, protect code blocks from processing
    code_blocks = []
    def save_code_block(match):
        code_blocks.append(match.group(0))
        return f"__CODE_BLOCK_{len(code_blocks)-1}__"

    # Extract code blocks temporarily
    text = re.sub(r'```[\s\S]*?```', save_code_block, text)

    # Extract inline code temporarily
    inline_codes = []
    def save_inline_code(match):
        inline_codes.append(match.group(0))
        return f"__INLINE_CODE_{len(inline_codes)-1}__"

    text = re.sub(r'`[^`]+`', save_inline_code, text)

    # Now process the rest
    # Convert headers to bold: ## Header -> *Header*
    text = re.sub(r'^#{1,6}\s+(.+?)$', r'*\1*', text, flags=re.MULTILINE)

    # Convert **bold** to *bold* (Telegram uses single asterisk)
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)

    # Convert bullet points to simple bullets
    text = re.sub(r'^[\*\-]\s+', '• ', text, flags=re.MULTILINE)

    # Restore inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(f"__INLINE_CODE_{i}__", code)

    # Restore code blocks
    for i, block in enumerate(code_blocks):
        text = text.replace(f"__CODE_BLOCK_{i}__", block)

    return text


def strip_all_markdown(text: str) -> str:
    """
    Remove all markdown formatting, returning plain text.
    Use as fallback if markdown parsing fails.
    """
    if not text:
        return text

    # Remove code blocks
    text = re.sub(r'```[\s\S]*?```', '[code block]', text)

    # Remove inline code
    text = re.sub(r'`([^`]+)`', r'\1', text)

    # Remove bold
    text = re.sub(r'\*\*?(.+?)\*\*?', r'\1', text)

    # Remove italic
    text = re.sub(r'_(.+?)_', r'\1', text)

    # Remove links
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)

    # Remove headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)

    return text
