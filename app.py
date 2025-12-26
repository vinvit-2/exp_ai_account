import json
import time
import uuid
from pathlib import Path
import requests
import streamlit as st

# -----------------------------
# Experiment parameters
# -----------------------------
N_CANDIDATES = 12
SHORTLIST_CAP_K = 5
EXPECTED_MINUTES = 12  # soft expectation (non-blocking nudge)

CANDIDATES_PATH = Path(__file__).parent / "candidates.json"

# Bias configuration (only used in BIASED condition)
BIAS_DELTA = 6            # score bonus for favored group
AI_THRESHOLD = 70         # recommend invite if score >= threshold
BIASED_ONLY_ON_BORDERLINE = True  # keep bias interpretation clean

# -----------------------------
# Utility
# -----------------------------
def now_ms() -> int:
    return int(time.time() * 1000)

def new_participant_id() -> str:
    return uuid.uuid4().hex

def stable_seed_int(hex_id: str) -> int:
    # deterministic given participant_id so refresh doesn't change condition
    return int(hex_id[:8], 16)

def assign_condition(seed: int):
    # 2x2: API x ALG (deterministic from seed)
    # Ensures equal-ish distribution over many participants
    api = "HIGH_API" if (seed % 2 == 0) else "LOW_API"
    alg = "BIASED" if ((seed // 2) % 2 == 0) else "JOB_MATCH"
    return api, alg

def load_candidates():
    data = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
    if len(data) != N_CANDIDATES:
        raise ValueError(f"candidates.json must contain exactly {N_CANDIDATES} candidates; got {len(data)}.")
    return data

def rubric_ground_truth(candidate) -> str:
    """
    Hidden deterministic 'ground truth' for rubric error computations.
    Keep it simple and aligned with your clear_pos/clear_neg tags.

    Here, we use a simple rule from card fields:
      Invite if RequirementsMet=Yes AND Portfolio=Yes AND YearsExperience >= 4 AND (Education in {MSc, PhD} OR Certifications >= 1)
    """
    c = candidate["card"]
    req = (str(c.get("RequirementsMet", "")).lower() == "yes")
    port = (str(c.get("Portfolio", "")).lower() == "yes")
    years = int(c.get("YearsExperience", 0))
    edu = str(c.get("Education", ""))
    certs = int(c.get("Certifications", 0))

    invite = req and port and (years >= 4) and ((edu in ["MSc", "PhD"]) or (certs >= 1))
    return "INVITE" if invite else "NO_INVITE"

def ai_score_and_rec(candidate, condition_alg: str):
    """
    Simple synthetic AI behavior:
      - base score from qualifications only
      - in BIASED condition, add +BIAS_DELTA to favored group on borderline cases (recommended)
    """
    c = candidate["card"]
    years = int(c.get("YearsExperience", 0))
    certs = int(c.get("Certifications", 0))
    edu = str(c.get("Education", ""))

    # Base score = qualifications
    score = 55
    score += min(years, 8) * 4
    score += min(certs, 3) * 4
    score += 8 if edu in ["MSc", "PhD"] else 0
    score += 6 if str(c.get("Portfolio", "")).lower() == "yes" else 0
    score += 6 if str(c.get("RequirementsMet", "")).lower() == "yes" else -6

    # Apply bias
    if condition_alg == "BIASED":
        if (not BIASED_ONLY_ON_BORDERLINE) or (candidate.get("case_type") == "borderline"):
            if candidate.get("demo_group") == "favored":
                score += BIAS_DELTA

    score = int(max(0, min(100, round(score))))
    rec = "INVITE" if score >= AI_THRESHOLD else "NO_INVITE"

    # Simple top factors (optional display)
    top3 = [
        f"Years experience: {years}",
        f"Education: {edu}",
        f"Certifications: {certs}"
    ]
    return {"score": score, "rec": rec, "top3": top3}

def post_log(event_type: str, payload: dict, trial_index=None, candidate_id=None):
    """
    Logs to Google Sheets via Apps Script Web App.
    Expects Streamlit Secrets:
      LOG_URL
      LOG_KEY
    """
    url = st.secrets.get("LOG_URL", "")
    key = st.secrets.get("LOG_KEY", "")
    if not url or not key:
        # If secrets aren't set, don't crash. Useful for local testing.
        return

    body = {
        "key": key,
        "ts_ms": now_ms(),
        "participant_id": st.session_state.participant_id,
        "condition_api": st.session_state.condition_api,
        "condition_alg": st.session_state.condition_alg,
        "trial_index": trial_index,
        "candidate_id": candidate_id,
        "event_type": event_type,
        "payload": payload,
    }
    try:
        requests.post(url, json=body, timeout=6)
    except Exception:
        pass

# -----------------------------
# Streamlit app
# -----------------------------
st.set_page_config(page_title="AI Hiring Task", layout="wide")

# Init session
if "participant_id" not in st.session_state:
    st.session_state.participant_id = new_participant_id()
    seed = stable_seed_int(st.session_state.participant_id)
    api, alg = assign_condition(seed)
    st.session_state.condition_api = api
    st.session_state.condition_alg = alg

    st.session_state.candidates = load_candidates()

    # Randomize order within participant but keep deterministic from seed
    # (Prevents people inferring patterns; stable across refresh)
    rnd = seed
    order = list(range(N_CANDIDATES))
    # simple deterministic shuffle
    for i in range(N_CANDIDATES - 1, 0, -1):
        rnd = (1103515245 * rnd + 12345) & 0x7fffffff
        j = rnd % (i + 1)
        order[i], order[j] = order[j], order[i]
    st.session_state.order = order

    st.session_state.trial_index = 0
    st.session_state.shortlisted = 0
    st.session_state.task_start_ms = now_ms()
    st.session_state.trial_start_ms = now_ms()

    # Decision gating
    st.session_state.decision_locked = False
    st.session_state.pending_justification = False
    st.session_state.last_ai = None
    st.session_state.last_candidate_id = None
    st.session_state.details_open_ms = None

    post_log("session_start", {"soft_expected_minutes": EXPECTED_MINUTES})

pid = st.session_state.participant_id
api = st.session_state.condition_api
alg = st.session_state.condition_alg
ti = st.session_state.trial_index

# Header
st.title("AI-supported Hiring Decisions")
elapsed_min = (now_ms() - st.session_state.task_start_ms) / 60000.0

st.caption(
    f"Trial {ti+1}/{N_CANDIDATES} | Invited: {st.session_state.shortlisted}/{SHORTLIST_CAP_K} | "
    f"Elapsed: {elapsed_min:.1f} min"
)

# Soft time nudge
if elapsed_min > EXPECTED_MINUTES:
    st.warning("Please continue at a steady pace. Most people finish in about 10–12 minutes.")

# Completion
if ti >= N_CANDIDATES:
    code = pid[-8:].upper()
    st.success(f"Completed. Completion code: {code}")
    post_log("session_end", {"completion_code": code, "elapsed_min": elapsed_min})
    st.stop()

# Current candidate
cand_idx = st.session_state.order[ti]
cand = st.session_state.candidates[cand_idx]

ai = ai_score_and_rec(cand, alg)
truth = rubric_ground_truth(cand)

# Log trial start (once per trial)
if st.session_state.last_candidate_id != cand["candidate_id"]:
    st.session_state.trial_start_ms = now_ms()
    st.session_state.last_ai = ai
    st.session_state.last_candidate_id = cand["candidate_id"]
    st.session_state.details_open_ms = None
    post_log("trial_start", {
        "case_type": cand.get("case_type"),
        "pair_id": cand.get("pair_id"),
        "demo_group": cand.get("demo_group"),
        "ai_score": ai["score"],
        "ai_rec": ai["rec"],
        "rubric_truth": truth
    }, trial_index=ti, candidate_id=cand["candidate_id"])

# Layout
col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("Candidate (CV card)")
    card = cand["card"]
    for k, v in card.items():
        st.write(f"**{k}:** {v}")
    st.write(f"_{cand.get('summary_paragraph','')}_")

    # Details toggle (measurable open/close)
    open_details = st.toggle("View full CV", value=False, key=f"details_{cand['candidate_id']}")
    if open_details:
        # log open once
        if st.session_state.details_open_ms is None:
            st.session_state.details_open_ms = now_ms()
            post_log("detail_open", {}, trial_index=ti, candidate_id=cand["candidate_id"])
        st.divider()
        st.write(cand.get("full_cv", ""))
    else:
        # if closing after being open, log duration
        if st.session_state.details_open_ms is not None:
            dur_ms = now_ms() - st.session_state.details_open_ms
            st.session_state.details_open_ms = None
            post_log("detail_close", {"duration_ms": dur_ms}, trial_index=ti, candidate_id=cand["candidate_id"])

with col2:
    st.subheader("AI output")
    st.write(f"**Fit score:** {ai['score']} / 100")
    st.write(f"**Recommendation:** {'Interview' if ai['rec']=='INVITE' else 'Not interview'}")

    # Optional: show factors only in HIGH_API (keeps manipulation clean)
    if api == "HIGH_API":
        with st.expander("Top factors (model explanation)"):
            for t in ai["top3"]:
                st.write(f"- {t}")

    # High API: model traceability + audit cue
    if api == "HIGH_API":
        with st.expander("Model info (traceability)"):
            st.write("Model version: v1.0 (synthetic)")
            st.write("Validation accuracy: 0.82 (illustrative)")
            st.write("Fairness metric (selection rate diff): 0.06 (illustrative)")
            st.write("Feature importance (illustrative): experience, education, certifications")
        st.caption("Decisions may be audited for compliance and fairness.")

with col3:
    st.subheader("Your decision")

    if api == "HIGH_API":
        if st.button("Flag for independent review"):
            post_log("flag_review", {"reason": "user_flag"}, trial_index=ti, candidate_id=cand["candidate_id"])
            st.info("Flagged for review.")

    # Decision buttons
    invite_disabled = st.session_state.decision_locked or (st.session_state.shortlisted >= SHORTLIST_CAP_K)
    reject_disabled = st.session_state.decision_locked

    cA, cB = st.columns(2)
    with cA:
        invite_clicked = st.button("Invite", disabled=invite_disabled)
    with cB:
        reject_clicked = st.button("Do not invite", disabled=reject_disabled)

    if st.session_state.shortlisted >= SHORTLIST_CAP_K and not st.session_state.decision_locked:
        st.info("You have reached the maximum number of interview invites.")

    # Handle decision
    if invite_clicked or reject_clicked:
        decision = "INVITE" if invite_clicked else "NO_INVITE"
        rt_ms = now_ms() - st.session_state.trial_start_ms

        agree = (decision == ai["rec"])
        override = (not agree)

        # Update shortlist
        if decision == "INVITE":
            st.session_state.shortlisted += 1

        post_log("decision", {
            "decision": decision,
            "ai_rec": ai["rec"],
            "agree": agree,
            "override": override,
            "rt_ms": rt_ms,
            "ai_score": ai["score"],
            "rubric_truth": truth,
            "rubric_error": (decision != truth),
            "case_type": cand.get("case_type"),
            "pair_id": cand.get("pair_id"),
            "demo_group": cand.get("demo_group"),
        }, trial_index=ti, candidate_id=cand["candidate_id"])

        st.session_state.decision_locked = True

        # High API: require justification only when overriding AI
        if api == "HIGH_API" and override:
            st.session_state.pending_justification = True

    # Justification gate
    if st.session_state.pending_justification:
        st.write("**Brief justification (1 sentence):**")
        txt = st.text_area("Justification", height=80, key=f"just_{cand['candidate_id']}")
        if st.button("Submit justification"):
            if len(txt.strip()) < 5:
                st.warning("Please write one short sentence.")
            else:
                post_log("override_justification", {"text": txt.strip()},
                         trial_index=ti, candidate_id=cand["candidate_id"])
                st.session_state.pending_justification = False
                st.success("Justification saved.")

    # Next button (blocked until justification submitted, if required)
    can_next = st.session_state.decision_locked and (not st.session_state.pending_justification)
    if st.button("Next candidate", disabled=not can_next):
        # If details currently open, close-log it to avoid missing durations
        if st.session_state.details_open_ms is not None:
            dur_ms = now_ms() - st.session_state.details_open_ms
            st.session_state.details_open_ms = None
            post_log("detail_close", {"duration_ms": dur_ms, "auto_closed_on_next": True},
                     trial_index=ti, candidate_id=cand["candidate_id"])

        st.session_state.trial_index += 1
        st.session_state.decision_locked = False
        st.session_state.pending_justification = False
        st.session_state.last_candidate_id = None  # force trial_start log
        st.rerun()
