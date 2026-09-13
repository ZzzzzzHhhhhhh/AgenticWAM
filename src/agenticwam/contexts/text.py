"""Compile a validated atomic goal to a text-conditioned policy input."""

from agenticwam.core.types import Context, ContractError


class TextCompiler:
    def __init__(self, *, supported_skills=("language",), context_factory=Context):
        self.supported_skills = tuple(supported_skills)
        self.context_factory = context_factory

    def compile(self, step, context_id, revision):
        if step.skill not in self.supported_skills:
            raise ContractError(f"backend does not support skill {step.skill!r}")
        return self.context_factory(context_id, revision, step.instruction)
