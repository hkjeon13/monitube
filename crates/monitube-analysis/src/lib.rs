//! Bounded bag-of-words and pure-frequency analysis primitives.

mod tokenizer;

pub use tokenizer::{TokenizerClient, TokenizerClientError};

use std::collections::BTreeMap;
use thiserror::Error;

pub const MAX_UNIQUE_TERMS_PER_DOCUMENT: usize = 100_000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BagOfWords {
    terms: BTreeMap<String, u32>,
    token_count: u64,
}

impl BagOfWords {
    /// Builds a sparse term-frequency map from one token sequence.
    ///
    /// # Errors
    ///
    /// Returns an error when token or per-term counters overflow, or when the
    /// configured unique-term bound is exceeded.
    pub fn from_tokens<I>(tokens: I) -> Result<Self, BagOfWordsError>
    where
        I: IntoIterator<Item = String>,
    {
        let mut terms = BTreeMap::new();
        let mut token_count = 0_u64;

        for token in tokens {
            if token.is_empty() {
                continue;
            }
            token_count = token_count
                .checked_add(1)
                .ok_or(BagOfWordsError::TokenCountOverflow)?;
            let entry = terms.entry(token).or_insert(0_u32);
            *entry = entry
                .checked_add(1)
                .ok_or(BagOfWordsError::TermFrequencyOverflow)?;
            if terms.len() > MAX_UNIQUE_TERMS_PER_DOCUMENT {
                return Err(BagOfWordsError::TooManyUniqueTerms {
                    maximum: MAX_UNIQUE_TERMS_PER_DOCUMENT,
                });
            }
        }

        Ok(Self { terms, token_count })
    }

    #[must_use]
    pub const fn token_count(&self) -> u64 {
        self.token_count
    }

    #[must_use]
    pub fn terms(&self) -> &BTreeMap<String, u32> {
        &self.terms
    }

    #[must_use]
    pub fn delta_from(&self, previous: &Self) -> BagOfWordsDelta {
        let mut term_frequency = BTreeMap::new();

        for (term, frequency) in &previous.terms {
            term_frequency.insert(term.clone(), -i64::from(*frequency));
        }
        for (term, frequency) in &self.terms {
            let delta = term_frequency.entry(term.clone()).or_insert(0);
            *delta += i64::from(*frequency);
        }
        term_frequency.retain(|_, delta| *delta != 0);

        BagOfWordsDelta {
            token_count: signed_difference(self.token_count, previous.token_count),
            term_frequency,
        }
    }
}

fn signed_difference(current: u64, previous: u64) -> i128 {
    i128::from(current) - i128::from(previous)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BagOfWordsDelta {
    pub token_count: i128,
    pub term_frequency: BTreeMap<String, i64>,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum BagOfWordsError {
    #[error("document token count overflowed")]
    TokenCountOverflow,
    #[error("term frequency overflowed")]
    TermFrequencyOverflow,
    #[error("document contains more than {maximum} unique terms")]
    TooManyUniqueTerms { maximum: usize },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrequencyAggregate {
    pub term: String,
    pub total_term_frequency: u64,
    pub document_frequency: u64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct FrequencyResult {
    pub term: String,
    pub term_count: u64,
    pub document_count: u64,
    pub document_rate: f64,
}

#[must_use]
pub fn rank_by_frequency(
    rows: impl IntoIterator<Item = FrequencyAggregate>,
    document_count: u64,
    limit: usize,
) -> Vec<FrequencyResult> {
    let mut ranked = rows
        .into_iter()
        .filter(|row| !row.term.is_empty() && row.total_term_frequency > 0)
        .map(|row| FrequencyResult {
            term: row.term,
            term_count: row.total_term_frequency,
            document_count: row.document_frequency,
            document_rate: document_rate(row.document_frequency, document_count),
        })
        .collect::<Vec<_>>();

    ranked.sort_by(|left, right| {
        right
            .term_count
            .cmp(&left.term_count)
            .then_with(|| left.term.cmp(&right.term))
    });
    ranked.truncate(limit);
    ranked
}

fn document_rate(document_frequency: u64, document_count: u64) -> f64 {
    if document_count == 0 {
        return 0.0;
    }

    let bounded_frequency = document_frequency.min(document_count);
    let numerator = u128::from(bounded_frequency) * 10_000;
    let rounded_basis_points =
        (numerator + u128::from(document_count) / 2) / u128::from(document_count);
    let basis_points = u16::try_from(rounded_basis_points).unwrap_or(10_000);
    f64::from(basis_points) / 100.0
}

#[cfg(test)]
mod tests {
    use super::{BagOfWords, BagOfWordsError, FrequencyAggregate, rank_by_frequency};

    #[test]
    fn token_sequence_becomes_sparse_bag_of_words() -> Result<(), BagOfWordsError> {
        let bag = BagOfWords::from_tokens(["분석", "영상", "분석"].into_iter().map(str::to_owned))?;

        assert_eq!(bag.token_count(), 3);
        assert_eq!(bag.terms().get("분석"), Some(&2));
        assert_eq!(bag.terms().get("영상"), Some(&1));
        Ok(())
    }

    #[test]
    fn replacement_delta_removes_old_and_adds_new_counts() -> Result<(), BagOfWordsError> {
        let old = BagOfWords::from_tokens(["분석", "영상", "분석"].into_iter().map(str::to_owned))?;
        let new =
            BagOfWords::from_tokens(["분석", "데이터", "데이터"].into_iter().map(str::to_owned))?;

        let delta = new.delta_from(&old);
        assert_eq!(delta.token_count, 0);
        assert_eq!(delta.term_frequency.get("분석"), Some(&-1));
        assert_eq!(delta.term_frequency.get("영상"), Some(&-1));
        assert_eq!(delta.term_frequency.get("데이터"), Some(&2));
        Ok(())
    }

    #[test]
    fn ranking_uses_only_raw_frequency_then_term_for_stable_ties() {
        let ranked = rank_by_frequency(
            [
                FrequencyAggregate {
                    term: "beta".to_owned(),
                    total_term_frequency: 10,
                    document_frequency: 4,
                },
                FrequencyAggregate {
                    term: "alpha".to_owned(),
                    total_term_frequency: 10,
                    document_frequency: 3,
                },
                FrequencyAggregate {
                    term: "gamma".to_owned(),
                    total_term_frequency: 7,
                    document_frequency: 4,
                },
            ],
            8,
            2,
        );

        assert_eq!(ranked.len(), 2);
        assert_eq!(ranked[0].term, "alpha");
        assert!((ranked[0].document_rate - 37.5).abs() < f64::EPSILON);
        assert_eq!(ranked[1].term, "beta");
    }
}
