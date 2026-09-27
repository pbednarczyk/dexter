from copy import deepcopy

from src.domain.llm import Message


class MockLLMProvider:
    def __init__(self, replies: list[Message]):
        self.replies = iter(replies)
        self.requests = []

    async def chat(self, messages, tools):
        self.requests.append((deepcopy(messages), deepcopy(tools)))
        return next(self.replies)
