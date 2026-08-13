//! PostgreSQL-safe normalization for provider-owned text values.

use std::borrow::Cow;

/// `PostgreSQL` text cannot contain NUL. Preserve every other Unicode scalar.
pub fn strip_nul(value: &str) -> Cow<'_, str> {
    if value.contains('\0') {
        Cow::Owned(value.replace('\0', ""))
    } else {
        Cow::Borrowed(value)
    }
}

pub fn optional(value: Option<&str>) -> Option<String> {
    value.map(strip_nul).map(Cow::into_owned)
}

pub fn required(value: &str) -> Option<String> {
    let cleaned = strip_nul(value);
    (!cleaned.is_empty()).then(|| cleaned.into_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn removes_only_nul() {
        assert_eq!(strip_nul("가\0나\n다"), "가나\n다");
    }

    #[test]
    fn rejects_identifier_that_becomes_empty() {
        assert_eq!(required("\0"), None);
        assert_eq!(required("abc\0def").as_deref(), Some("abcdef"));
    }
}
