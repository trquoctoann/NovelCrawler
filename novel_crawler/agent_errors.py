"""Explicit provider refusals are chapter failures, not account outages."""


class AgentResponseError(RuntimeError):
    def __init__(self, message, usage=None):
        super().__init__(message)
        self.usage = usage


class ContentPolicyError(AgentResponseError):
    pass


def policy_message(message):
    text = str(message).lower()
    return any(marker in text for marker in (
        'blocked by content safety', 'content policy', 'content_policy',
        'content_filter', 'safety policy', 'prohibited use policy',
        'prohibited_content', 'finish_reason: safety', 'finishreason: safety',
    )) or text.lstrip().startswith('policy:')


def provider_error(message, usage=None):
    return (ContentPolicyError if policy_message(message) else AgentResponseError)(message, usage)
