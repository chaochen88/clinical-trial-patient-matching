"""Source-linked screening rules; extraction never determines patient eligibility."""

import calendar
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "0.1"
Result = Literal["met", "not_met", "unknown", "requires_review"]


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")
    months: int = Field(gt=0)
    anchor: Literal["screening"] = "screening"
    boundary: Literal["inclusive", "requires_review"] = "requires_review"


class Rule(BaseModel):
    """A recursive rule with explicit forms and shared bullet identity."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    kind: Literal["range", "threshold", "duration", "condition", "group"]
    source_bullet_id: str
    criterion_type: Literal["inclusion", "exclusion"]
    source_quote: str = Field(min_length=1)
    concept: str = Field(min_length=1)
    lower: float | None = None
    upper: float | None = None
    lower_inclusive: bool | None = None
    upper_inclusive: bool | None = None
    operator: Literal["lt", "le", "gt", "ge"] | None = None
    value: float | None = None
    unit: str | None = None
    uninterrupted: bool | None = None
    window: Window | None = None
    logic: Literal["any", "all"] | None = None
    children: list["Rule"] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_form(self):
        if self.kind == "range":
            if any(
                v is None
                for v in (
                    self.lower,
                    self.upper,
                    self.lower_inclusive,
                    self.upper_inclusive,
                    self.unit,
                )
            ):
                raise ValueError("range requires bounds, inclusivity, and unit")
            if self.lower > self.upper:
                raise ValueError("lower must be <= upper")
        if self.kind in {"threshold", "duration"}:
            if self.operator is None or self.value is None or not self.unit:
                raise ValueError("threshold/duration requires operator, value, unit")
        if self.kind == "duration":
            if self.value < 0 or self.uninterrupted is None:
                raise ValueError("duration requires nonnegative value and continuity")
        if self.kind == "group":
            if self.logic is None or len(self.children) < 2:
                raise ValueError("group requires logic and at least two children")
            for child in self.children:
                if (
                    child.source_bullet_id != self.source_bullet_id
                    or child.criterion_type != self.criterion_type
                ):
                    raise ValueError("children must retain bullet ID and polarity")
        elif self.children or self.logic is not None:
            raise ValueError("only groups may contain children or logic")
        allowed = {
            "range": {"lower", "upper", "lower_inclusive", "upper_inclusive", "unit"},
            "threshold": {"operator", "value", "unit"},
            "duration": {"operator", "value", "unit", "uninterrupted"},
            "condition": set(),
            "group": {"logic"},
        }[self.kind]
        for field in {
            "lower",
            "upper",
            "lower_inclusive",
            "upper_inclusive",
            "operator",
            "value",
            "unit",
            "uninterrupted",
            "logic",
        } - allowed:
            if getattr(self, field) is not None:
                raise ValueError(f"{field} is invalid for {self.kind}")
        return self


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[Rule]
    review_findings: list[str]


def validate_source(rule: Rule, bullet_id: str, polarity: str, text: str) -> None:
    if rule.source_bullet_id != bullet_id or rule.criterion_type != polarity:
        raise ValueError("source identity or polarity changed")
    if rule.source_quote not in text:
        raise ValueError("quote is not an exact substring of assigned bullet")
    for child in rule.children:
        validate_source(child, bullet_id, polarity, text)


def evaluate_numeric(rule: Rule, value: float | None, unit: str | None) -> Result:
    if value is None:
        return "unknown"
    if unit != rule.unit:
        return "requires_review"
    if rule.kind == "range":
        low = value >= rule.lower if rule.lower_inclusive else value > rule.lower
        high = value <= rule.upper if rule.upper_inclusive else value < rule.upper
        applies = low and high
    elif rule.kind == "threshold":
        applies = {
            "lt": value < rule.value,
            "le": value <= rule.value,
            "gt": value > rule.value,
            "ge": value >= rule.value,
        }[rule.operator]
    else:
        return "requires_review"
    if rule.criterion_type == "exclusion":
        applies = not applies
    return "met" if applies else "not_met"


def combine(evidence: list[bool | None], logic: str) -> bool | None:
    """Three-valued applicability, before applying exclusion polarity."""
    if not evidence or logic not in {"any", "all"}:
        raise ValueError("expected nonempty evidence and any/all logic")
    if logic == "any":
        return True if True in evidence else (None if None in evidence else False)
    return False if False in evidence else (None if None in evidence else True)


def screening_result(applies: bool | None, polarity: str) -> Result:
    if applies is None:
        return "unknown"
    passed = applies if polarity == "inclusion" else not applies
    return "met" if passed else "not_met"


def months_before(anchor: date, months: int) -> date:
    index = anchor.year * 12 + anchor.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def in_window(event: date | None, screening: date, window: Window) -> bool | None:
    """Closed interval [screening - calendar months, screening] when reviewed."""
    if event is None or window.boundary == "requires_review":
        return None
    return months_before(screening, window.months) <= event <= screening
