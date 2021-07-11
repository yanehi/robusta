import json
import logging
import os
import tempfile
from typing import Dict, Callable, Any, List

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from ...core.model.events import *
from ...core.reporting.blocks import *
from ...core.reporting.utils import add_pngs_for_all_svgs
from ...core.reporting.callbacks import PlaybookCallbackRequest, callback_registry
from .receiver import TARGET_ID

SLACK_TOKEN = os.getenv("SLACK_TOKEN")
ACTION_TRIGGER_PLAYBOOK = "trigger_playbook"

# TODO: we need to make this modular so you can plug n' play different report receivers (slack, msteams, etc)
# a first step in that direction would be to move all the functions here to a class like SlackReceiver
# which inherits from an abstract base class ReportReceiver

slack_client = WebClient(token=SLACK_TOKEN)


def start_slack_sender() -> bool:
    """
    Connect to Slack and verify that the Slack token is valid.
    Return True on success, False on failure
    """
    try:
        slack_client.auth_test()
        return True
    except SlackApiError as e:
        logging.error(f"Cannot connect to Slack API: {e}")
        return False


def get_action_block_for_choices(choices: Dict[str, Callable] = None, context=""):
    if choices is None:
        return []

    buttons = []
    for (i, (text, callback)) in enumerate(choices.items()):
        if callback is None:
            raise Exception(
                f"The callback for choice {text} is None. Did you accidentally pass `foo()` as a callback and not `foo`?"
            )
        if not callback_registry.is_callback_in_registry(callback):
            raise Exception(
                f"{callback} is not a function that was decorated with @on_report_callback or it somehow"
                f" has the wrong version (e.g. multiple functions with the same name were decorated "
                f"with @on_report_callback)"
            )
        buttons.append(
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": text,
                },
                "style": "primary",
                "action_id": f"{ACTION_TRIGGER_PLAYBOOK}_{i}",
                "value": PlaybookCallbackRequest.create_for_func(
                    callback, context
                ).json(),
            }
        )

    return [{"type": "actions", "elements": buttons}]


def apply_length_limit(msg: str, max_length: int = 3000):
    if len(msg) <= max_length:
        return msg
    truncator = "..."
    return msg[: max_length - len(truncator)] + truncator


SlackBlock = Dict[str, Any]


def to_slack(block: BaseBlock) -> List[SlackBlock]:
    if isinstance(block, MarkdownBlock):
        if not block.text:
            return []
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": apply_length_limit(block.text),
                },
            }
        ]
    elif isinstance(block, DividerBlock):
        return [{"type": "divider"}]
    elif isinstance(block, FileBlock):
        raise AssertionError("to_slack() should never be called on a FileBlock")
    elif isinstance(block, HeaderBlock):
        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": apply_length_limit(block.text, 150),
                },
            }
        ]
    elif isinstance(block, ListBlock) or isinstance(block, TableBlock):
        return to_slack(block.to_markdown())
    elif isinstance(block, CallbackBlock):
        context = block.context.copy()
        context["target_id"] = TARGET_ID
        return get_action_block_for_choices(block.choices, json.dumps(context))
    else:
        logging.error(f"cannot convert block of type {type(block)} to slack format")
        return []  # no reason to crash the entire report


def upload_file_to_slack(block: FileBlock) -> str:
    """Upload a file to slack and return a link to it"""
    with tempfile.NamedTemporaryFile() as f:
        f.write(block.contents)
        f.flush()
        result = slack_client.files_upload(
            title=block.filename, file=f.name, filename=block.filename
        )
        return result["file"]["permalink"]


def prepare_slack_text(
    message: str, mentions: List[str] = [], files: List[FileBlock] = []
):
    """Adds mentions and truncates text if it is too long."""
    mention_prefix = " ".join([f"<@{user_id}>" for user_id in mentions])
    if mention_prefix != "":
        message = f"{mention_prefix} {message}"
    if files:
        # it's a little annoying but it seems like files need to be referenced in `title` and not just `blocks`
        # in order to be actually shared. well, I'm actually not sure about that, but when I tried adding the files
        # to a separate block and not including them in `title` or the first block then the link was present but
        # the file wasn't actually shared and the link was broken
        uploaded_files = []
        for file_block in files:
            permalink = upload_file_to_slack(file_block)
            uploaded_files.append(f"* <{permalink} | {file_block.filename}>")

        file_references = "\n".join(uploaded_files)
        message = f"{message}\n{file_references}"

    if len(message) == 0:
        return "empty-message"  # blank messages aren't allowed

    return apply_length_limit(message)


def send_to_slack(event: BaseEvent):
    file_blocks = add_pngs_for_all_svgs(
        [b for b in event.report_blocks if isinstance(b, FileBlock)]
    )
    other_blocks = [b for b in event.report_blocks if not isinstance(b, FileBlock)]

    message = prepare_slack_text(event.report_title, event.slack_mentions, file_blocks)

    output_blocks = []
    if not event.report_title_hidden and event.report_title:
        output_blocks.extend(to_slack(HeaderBlock(event.report_title)))
    for block in other_blocks:
        output_blocks.extend(to_slack(block))
    attachment_blocks = []
    for block in event.report_attachment_blocks:
        attachment_blocks.extend(to_slack(block))

    logging.debug(
        f"--sending to slack--\n"
        f"title:{event.report_title}\n"
        f"blocks: {output_blocks}\n"
        f"attachment_blocks: {event.report_attachment_blocks}\n"
        f"message:{message}"
    )

    try:
        if attachment_blocks:
            slack_client.chat_postMessage(
                channel=event.slack_channel,
                text=message,
                blocks=output_blocks,
                display_as_bot=True,
                attachments=[{"blocks": attachment_blocks}],
                unfurl_links=event.slack_allow_unfurl,
                unfurl_media=event.slack_allow_unfurl,
            )
        else:
            slack_client.chat_postMessage(
                channel=event.slack_channel,
                text=message,
                blocks=output_blocks,
                display_as_bot=True,
                unfurl_links=event.slack_allow_unfurl,
                unfurl_media=event.slack_allow_unfurl,
            )
    except Exception as e:
        logging.error(
            f"error sending message to slack\ne={e}\ntext={message}\nblocks={output_blocks}\nattachment_blocks={attachment_blocks}"
        )
