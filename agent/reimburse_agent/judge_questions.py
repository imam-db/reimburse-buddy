"""Every Jev question, criterion and threshold the judge uses, in one place (audited and edited together).

Rules followed (TypeSafe docs, jev-1.13 jaggedness page, read 2026-10-25):
- one judgment per question, phrased so yes = the risky case; true/false criteria as concrete situations
- no dates, numbers or arithmetic in a question: code computes weekday names and compares times
- one candidate message per request, only the fields a person would look at (no irrelevant state)
- Nouls only (no Choice, so no option-order bias); message text is data, never instructions
"""

MODEL = "jev-1.13.0"
URL = "https://ai.sumopod.com/v1/systemone"
KEY_ENV = "LLM_GATEWAY_API_KEY"
ENABLE_ENV = "RB_JUDGE"  # "jev" turns the judge on; anything else (default) = NullJudge
TIMEOUT_S = 3.0
YES = 0.7  # >= YES: the risky reading holds -> review flag
NO = 0.3   # (NO, YES): unsettled -> review flag as well (escalation only, so doubt escalates)
FLAG_ON_ERROR = False  # a failed call is recorded and never read as "no"; it does not flag by itself because
                       # the deterministic guards stay primary. Set True to escalate on every judge failure.

QUESTIONS = {
    "meeting_moved": {
        "type": "noul",
        "instructions": "Does `message` say that `meeting` now happens at a different time than `meeting.scheduled_time`?",
        "criteria": {"true": "The message gives a new time for this meeting, or says it was moved back to an earlier plan.",
                     "false": "The message keeps the meeting time, is about a different meeting, or is not about a meeting."},
    },
    "meeting_cancelled": {
        "type": "noul",
        "instructions": "Does `message` say that `meeting` will not take place?",
        "criteria": {"true": "The message says this meeting was cancelled or called off.",
                     "false": "The message says the meeting still happens, or is about something else."},
    },
    "more_to_follow": {
        "type": "noul",
        "instructions": "Does `message` say that the sender will send some document later?",
        "criteria": {"true": "The sender promises another receipt or document that is not attached yet.",
                     "false": "The sender sends everything now, or promises nothing."},
    },
    "personal_reason": {
        "type": "noul",
        "instructions": "Does `message` give a personal reason, not a work reason, for the late hotel check-out?",
        "criteria": {"true": "The reason is private, such as oversleeping, rest, leisure or family.",
                     "false": "The reason is work, such as a client meeting, a signing, or waiting for a customer."},
    },
    "describes_other_ride": {
        "type": "noul",
        "instructions": "Does `message` describe a ride with a different route than `item.route`?",
        "criteria": {"true": "The message names a start or destination that differs from the item's route.",
                     "false": "The message names the same route, or names no route."},
    },
}

# Which questions apply to which open item, and what a risky answer means for review.
BY_CATEGORY = {"late_checkout": ["meeting_moved", "meeting_cancelled", "personal_reason"],
               "taxi": ["describes_other_ride", "more_to_follow"],
               "hotel": ["more_to_follow"]}
