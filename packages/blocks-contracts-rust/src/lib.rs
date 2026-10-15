use serde_json::Value;

fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str, String> {
    v.get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && s.len() <= 256)
        .ok_or_else(|| format!("missing/invalid {key}"))
}
fn require(ok: bool, message: &str) -> Result<(), String> {
    if ok {
        Ok(())
    } else {
        Err(message.into())
    }
}
fn uuid(s: &str) -> bool {
    s.len() == 36
        && s.bytes().enumerate().all(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                b == b'-'
            } else {
                b.is_ascii_hexdigit()
            }
        })
}
fn timestamp(s: &str) -> bool {
    // Backend emits UTC ISO-8601; do not silently admit arbitrary date strings.
    if !s.is_ascii() {
        return false;
    }
    let Some(body) = s.strip_suffix("+00:00").or_else(|| s.strip_suffix('Z')) else {
        return false;
    };
    if body.len() < 19
        || &body[4..5] != "-"
        || &body[7..8] != "-"
        || &body[10..11] != "T"
        || &body[13..14] != ":"
        || &body[16..17] != ":"
    {
        return false;
    }
    let number = |a: usize, b: usize| body.get(a..b).and_then(|v| v.parse::<u32>().ok());
    let (Some(y), Some(m), Some(d), Some(h), Some(n), Some(sec)) = (
        number(0, 4),
        number(5, 7),
        number(8, 10),
        number(11, 13),
        number(14, 16),
        number(17, 19),
    ) else {
        return false;
    };
    let days = match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if y % 4 == 0 && (y % 100 != 0 || y % 400 == 0) {
                29
            } else {
                28
            }
        }
        _ => 0,
    };
    y > 0
        && d > 0
        && d <= days
        && h < 24
        && n < 60
        && sec < 60
        && (body.len() == 19
            || (body.as_bytes()[19] == b'.'
                && body.len() > 20
                && body[20..].bytes().all(|b| b.is_ascii_digit())))
}

/// Validate one exact scoped metadata event; never accept telemetry as proof of truth.
pub fn validate_event(v: &Value) -> Result<(), String> {
    let map = v.as_object().ok_or("event must be an object")?;
    let coverage = text(v, "coverage")?;
    let mutation = match coverage {
        "committed_mutations_only" => true,
        "volatile_http_attempts" => false,
        _ => return Err("unsupported coverage".into()),
    };
    let common = [
        "event_id",
        "correlation_id",
        "block_id",
        "edition",
        "configuration_digest",
        "tenant_id",
        "actor_id",
        "actor_type",
        "human_involvement",
        "operation",
        "result",
        "occurred_at",
        "duration_ms",
        "cost",
        "privacy",
        "coverage",
    ];
    let extra: &[&str] = if mutation {
        &[
            "schema_version",
            "block_version",
            "feedback_id",
            "revision",
            "acceptance",
            "billability",
        ]
    } else {
        &["method", "http_status", "refusal_code"]
    };
    require(
        map.keys().all(|key| {
            common.contains(&key.as_str())
                || extra.contains(&key.as_str())
                || (mutation && key == "sequence")
        }),
        "unexpected field: raw payloads and secret fields are forbidden",
    )?;
    require(
        common
            .iter()
            .chain(extra.iter())
            .all(|key| map.contains_key(*key)),
        "missing required metadata field",
    )?;
    require(
        uuid(text(v, "event_id")?) && uuid(text(v, "correlation_id")?),
        "event/correlation identity must be UUID",
    )?;
    require(
        text(v, "block_id")? == "gradia.feedback",
        "unsupported block contract",
    )?;
    text(v, "edition")?;
    let config = text(v, "configuration_digest")?;
    require(
        config.len() == 64 && config.bytes().all(|b| b.is_ascii_hexdigit()),
        "configuration digest must be SHA-256 hex",
    )?;
    require(
        text(v, "privacy")? == "metadata_only",
        "unsupported privacy policy",
    )?;
    require(
        text(v, "human_involvement")? == "unknown",
        "unsubstantiated human involvement",
    )?;
    require(timestamp(text(v, "occurred_at")?), "invalid UTC timestamp")?;
    require(
        v["duration_ms"]
            .as_f64()
            .is_some_and(|n| n.is_finite() && n >= 0.0),
        "invalid duration",
    )?;
    match text(v, "actor_type")? {
        "configured_principal" => {
            text(v, "tenant_id")?;
            text(v, "actor_id")?;
        }
        "unauthenticated" => require(
            !mutation && v["tenant_id"].is_null() && v["actor_id"].is_null(),
            "unauthenticated identity must be null and volatile",
        )?,
        _ => return Err("unsupported actor type".into()),
    }
    let cost = v["cost"].as_object().ok_or("invalid cost")?;
    require(
        cost.len() == 3
            && ["status", "amount", "currency"]
                .iter()
                .all(|k| cost.contains_key(*k)),
        "unexpected cost fields",
    )?;
    match text(&v["cost"], "status")? {
        "unknown" => require(
            v["cost"]["amount"].is_null() && v["cost"]["currency"].is_null(),
            "unknown cost must remain null",
        )?,
        "measured" | "estimated" => {
            require(
                v["cost"]["amount"]
                    .as_f64()
                    .is_some_and(|n| n.is_finite() && n >= 0.0),
                "invalid cost amount",
            )?;
            let currency = text(&v["cost"], "currency")?;
            require(
                currency.len() == 3 && currency.bytes().all(|b| b.is_ascii_uppercase()),
                "currency must be three uppercase letters",
            )?;
        }
        _ => return Err("unsupported cost status".into()),
    }
    if mutation {
        if let Some(sequence) = v.get("sequence") {
            require(
                sequence.as_u64().is_some_and(|n| n > 0),
                "invalid event sequence",
            )?;
        }
        require(
            text(v, "schema_version")? == "1" && text(v, "block_version")? == text(v, "edition")?,
            "invalid mutation version",
        )?;
        require(
            uuid(text(v, "feedback_id")?) && v["revision"].as_u64().is_some_and(|n| n > 0),
            "invalid feedback identity/revision",
        )?;
        require(
            ["feedback.submitted", "feedback.triaged"].contains(&text(v, "operation")?)
                && text(v, "result")? == "persisted",
            "invalid mutation operation/result",
        )?;
        require(
            text(v, "acceptance")? == "not_evaluated" && text(v, "billability")? == "not_evaluated",
            "unsupported acceptance/billability claim",
        )?;
    } else {
        let operation = text(v, "operation")?;
        require(
            operation == "unmatched"
                || (operation.starts_with("/v1/feedback")
                    && operation
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"/{}_".contains(&b))),
            "operation must be a route template, not a raw URL",
        )?;
        require(
            ["GET", "POST", "PATCH", "PUT", "DELETE", "HEAD", "OPTIONS"]
                .contains(&text(v, "method")?),
            "unsupported HTTP method",
        )?;
        let status = v["http_status"].as_u64().ok_or("invalid HTTP status")?;
        require((100..=599).contains(&status), "invalid HTTP status")?;
        require(
            text(v, "result")?
                == if status < 400 {
                    "http_response"
                } else {
                    "http_refusal"
                },
            "HTTP result/status mismatch",
        )?;
        require(
            v["refusal_code"].is_null()
                || v["refusal_code"].as_str().is_some_and(|s| {
                    !s.is_empty()
                        && s.len() <= 128
                        && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
                }),
            "invalid refusal code",
        )?;
    }
    Ok(())
}

pub fn validate_document(v: &Value) -> Result<usize, String> {
    if let Some(events) = v.as_array() {
        require(
            !events.is_empty() && events.len() <= 4096,
            "event array must contain 1..4096 items",
        )?;
        for (i, event) in events.iter().enumerate() {
            validate_event(event).map_err(|e| format!("event {i}: {e}"))?;
        }
        Ok(events.len())
    } else {
        validate_event(v)?;
        Ok(1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> Value {
        serde_json::from_str(include_str!("../fixtures/python-events.json")).unwrap()
    }
    #[test]
    fn python_exports_match() {
        assert!(validate_document(&fixture()).unwrap() >= 3);
    }
    #[test]
    fn rejects_mutants() {
        let base = fixture()[0].clone();
        for (key, value) in [
            ("message", Value::from("raw private content")),
            ("duration_ms", Value::from(-1)),
            ("privacy", Value::from("raw")),
            ("coverage", Value::from("all")),
            ("configuration_digest", Value::from("bad")),
            ("occurred_at", Value::from("2026-02-30T00:00:00Z")),
            ("tenant_id", Value::Null),
        ] {
            let mut event = base.clone();
            event[key] = value;
            assert!(validate_event(&event).is_err(), "{key}");
        }
    }
    #[test]
    fn unknown_cost_is_not_zero() {
        let mut e = fixture()[0].clone();
        e["cost"]["amount"] = Value::from(0);
        assert!(validate_event(&e).is_err());
    }
    #[test]
    fn measured_cost_supported() {
        let mut e = fixture()[0].clone();
        e["cost"] = serde_json::json!({"status":"measured","amount":0.12,"currency":"USD"});
        assert!(validate_event(&e).is_ok());
        e["cost"]["amount"] = Value::from(-1);
        assert!(validate_event(&e).is_err());
    }
    #[test]
    fn missing_fields_rejected() {
        let mut e = fixture()[0].clone();
        e.as_object_mut().unwrap().remove("event_id");
        assert!(validate_event(&e).is_err());
    }
    #[test]
    fn malformed_documents_rejected() {
        assert!(validate_document(&serde_json::json!([])).is_err());
        assert!(validate_document(&serde_json::json!("event")).is_err());
    }
}
