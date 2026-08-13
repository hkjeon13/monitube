"use client";

import {
  EllipsisHorizontalIcon,
  PlusIcon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import type { FormEvent, KeyboardEvent as ReactKeyboardEvent } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  getAnalysisExcludedTerms,
  updateAnalysisExcludedTerms,
  type AnalysisKeywordCorpus,
  type FrequencyKeyword,
} from "../../lib/api";
import { formatCount } from "../collection/workbench-model";
import { useDialogFocusTrap } from "../collection/use-dialog-focus-trap";

type KeywordFrequencyPanelProps = {
  videoKeywords: FrequencyKeyword[];
  commentKeywords: FrequencyKeyword[];
  indexedVideoDocuments: number;
  indexedCommentDocuments: number;
  preferredCorpus: AnalysisKeywordCorpus;
  fullWidth: boolean;
  onSaved: () => void;
};

function normalizeTerm(value: string) {
  return value.normalize("NFC").trim().toLocaleLowerCase();
}

export function KeywordFrequencyPanel({
  videoKeywords,
  commentKeywords,
  indexedVideoDocuments,
  indexedCommentDocuments,
  preferredCorpus,
  fullWidth,
  onSaved,
}: KeywordFrequencyPanelProps) {
  const [corpus, setCorpus] = useState<AnalysisKeywordCorpus>(preferredCorpus);
  const [managerCorpus, setManagerCorpus] = useState<AnalysisKeywordCorpus>(preferredCorpus);
  const isVideo = corpus === "video";
  const keywords = isVideo ? videoKeywords : commentKeywords;
  const indexedDocumentCount = isVideo ? indexedVideoDocuments : indexedCommentDocuments;
  const label = isVideo ? "영상 대본" : "댓글";
  const managerIsVideo = managerCorpus === "video";
  const managerLabel = managerIsVideo ? "영상 대본" : "댓글";
  const managerKeywords = managerIsVideo ? videoKeywords : commentKeywords;
  const [menuOpen, setMenuOpen] = useState(false);
  const [managerOpen, setManagerOpen] = useState(false);
  const [excludedTerms, setExcludedTerms] = useState<string[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const videoTabRef = useRef<HTMLButtonElement>(null);
  const commentTabRef = useRef<HTMLButtonElement>(null);

  const closeManager = useCallback(() => {
    setManagerOpen(false);
    setInput("");
    setError(null);
    window.requestAnimationFrame(() => menuButtonRef.current?.focus());
  }, []);
  useDialogFocusTrap({ open: managerOpen, dialogRef, onClose: closeManager });

  useEffect(() => {
    if (!menuOpen) return;
    const dismiss = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
        menuButtonRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [menuOpen]);

  const availableTerms = useMemo(() => {
    const excluded = new Set(excludedTerms);
    return managerKeywords.map((keyword) => keyword.term).filter((term) => !excluded.has(term));
  }, [excludedTerms, managerKeywords]);

  const openManager = async () => {
    const selectedCorpus = corpus;
    setMenuOpen(false);
    setManagerCorpus(selectedCorpus);
    setManagerOpen(true);
    setLoading(true);
    setError(null);
    try {
      const result = await getAnalysisExcludedTerms();
      setExcludedTerms(selectedCorpus === "video" ? result.videoTerms : result.commentTerms);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "제외 키워드를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  };

  const addTerm = (rawTerm: string) => {
    const term = normalizeTerm(rawTerm);
    if (!term) return;
    setExcludedTerms((current) => current.includes(term) ? current : [...current, term]);
    setInput("");
  };

  const submitTerm = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    addTerm(input);
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await updateAnalysisExcludedTerms(managerCorpus, excludedTerms);
      closeManager();
      onSaved();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "제외 키워드를 저장하지 못했습니다.");
    } finally {
      setSaving(false);
    }
  };

  const selectTabFromKeyboard = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    let nextCorpus: AnalysisKeywordCorpus | null = null;
    if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextCorpus = isVideo ? "comment" : "video";
    if (event.key === "ArrowRight" || event.key === "ArrowDown") nextCorpus = isVideo ? "comment" : "video";
    if (event.key === "Home") nextCorpus = "video";
    if (event.key === "End") nextCorpus = "comment";
    if (!nextCorpus) return;
    event.preventDefault();
    setCorpus(nextCorpus);
    window.requestAnimationFrame(() => {
      (nextCorpus === "video" ? videoTabRef : commentTabRef).current?.focus();
    });
  };

  return (
    <section className={`analysis-panel analysis-panel-keywords${fullWidth ? " analysis-panel-keywords-wide" : ""}`}>
      <div className="analysis-panel-heading">
        <div>
          <p className="section-kicker">KEYWORD FREQUENCY</p>
          <h2>자주 등장하는 키워드</h2>
        </div>
        <div className="analysis-keyword-heading-actions">
          <span>{formatCount(indexedDocumentCount)}개 {isVideo ? "대본" : "댓글"}</span>
          <div className="analysis-keyword-menu-anchor" ref={menuRef}>
            <button
              ref={menuButtonRef}
              type="button"
              className="analysis-keyword-more"
              aria-label={`${label} 키워드 메뉴`}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((open) => !open)}
            >
              <EllipsisHorizontalIcon />
            </button>
            {menuOpen && (
              <div className="analysis-keyword-menu" role="menu">
                <button type="button" role="menuitem" onClick={() => void openManager()}>
                  제외 키워드 관리
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="analysis-keyword-tabs" role="tablist" aria-label="키워드 자료 유형">
        <button
          ref={videoTabRef}
          id="analysis-keyword-video-tab"
          type="button"
          role="tab"
          aria-selected={isVideo}
          aria-controls="analysis-keyword-panel"
          tabIndex={isVideo ? 0 : -1}
          className={isVideo ? "analysis-keyword-tab-active" : ""}
          onClick={() => setCorpus("video")}
          onKeyDown={selectTabFromKeyboard}
        >
          영상
        </button>
        <button
          ref={commentTabRef}
          id="analysis-keyword-comment-tab"
          type="button"
          role="tab"
          aria-selected={!isVideo}
          aria-controls="analysis-keyword-panel"
          tabIndex={isVideo ? -1 : 0}
          className={!isVideo ? "analysis-keyword-tab-active" : ""}
          onClick={() => setCorpus("comment")}
          onKeyDown={selectTabFromKeyboard}
        >
          댓글
        </button>
      </div>

      <div
        id="analysis-keyword-panel"
        role="tabpanel"
        aria-labelledby={`analysis-keyword-${corpus}-tab`}
        className="analysis-keyword-table-wrap"
      >
      {keywords.length ? (
        <table className="analysis-keyword-table">
          <thead>
            <tr>
              <th scope="col">순위</th>
              <th scope="col">키워드</th>
              <th scope="col">출현 횟수</th>
              <th scope="col">{isVideo ? "영상 수" : "댓글 수"}</th>
            </tr>
          </thead>
          <tbody>
            {keywords.map((keyword, index) => (
              <tr key={keyword.term}>
                <td>{index + 1}</td>
                <th scope="row">{keyword.term}</th>
                <td>{formatCount(keyword.termCount)}</td>
                <td>{formatCount(keyword.documentCount)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="analysis-empty">인덱싱된 {label} 키워드가 없습니다.</p>
      )}
      </div>

      {managerOpen && (
        <div className="analysis-keyword-dialog-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) closeManager();
        }}>
          <div
            ref={dialogRef}
            className="analysis-keyword-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby={`${managerCorpus}-excluded-keywords-title`}
            tabIndex={-1}
          >
            <header>
              <div>
                <p className="section-kicker">EXCLUDED KEYWORDS</p>
                <h2 id={`${managerCorpus}-excluded-keywords-title`}>{managerLabel} 제외 키워드</h2>
              </div>
              <button type="button" className="analysis-keyword-dialog-close" onClick={closeManager} aria-label="닫기">
                <XMarkIcon />
              </button>
            </header>
            <p className="analysis-keyword-dialog-description">
              제외한 단어는 {managerLabel} 빈도 순위에서 빠집니다. 아래 결과 단어를 누르거나 직접 입력하세요.
            </p>

            {error && <p className="analysis-keyword-dialog-error" role="alert">{error}</p>}
            {loading ? (
              <p className="analysis-keyword-dialog-state">제외 키워드를 불러오는 중입니다.</p>
            ) : (
              <>
                <form className="analysis-keyword-add" onSubmit={submitTerm}>
                  <label htmlFor={`${managerCorpus}-excluded-keyword-input`}>직접 추가</label>
                  <div>
                    <input
                      id={`${managerCorpus}-excluded-keyword-input`}
                      data-drawer-initial-focus
                      value={input}
                      maxLength={64}
                      placeholder="제외할 단어 입력"
                      onChange={(event) => setInput(event.target.value)}
                    />
                    <button type="submit" disabled={!input.trim()}><PlusIcon />추가</button>
                  </div>
                </form>

                <section className="analysis-keyword-manager-section" aria-labelledby={`${managerCorpus}-current-results-title`}>
                  <div className="analysis-keyword-manager-heading">
                    <h3 id={`${managerCorpus}-current-results-title`}>현재 결과에서 추가</h3>
                    <span>{availableTerms.length}개</span>
                  </div>
                  {availableTerms.length ? (
                    <div className="analysis-keyword-pills">
                      {availableTerms.map((term) => (
                        <button key={term} type="button" onClick={() => addTerm(term)}>
                          <PlusIcon />{term}
                        </button>
                      ))}
                    </div>
                  ) : <p className="analysis-keyword-manager-empty">추가할 현재 결과 단어가 없습니다.</p>}
                </section>

                <section className="analysis-keyword-manager-section" aria-labelledby={`${managerCorpus}-excluded-results-title`}>
                  <div className="analysis-keyword-manager-heading">
                    <h3 id={`${managerCorpus}-excluded-results-title`}>제외 중인 단어</h3>
                    <span>{excludedTerms.length}개</span>
                  </div>
                  {excludedTerms.length ? (
                    <div className="analysis-keyword-pills analysis-keyword-pills-excluded">
                      {excludedTerms.map((term) => (
                        <button
                          key={term}
                          type="button"
                          aria-label={`${term} 제외 해제`}
                          onClick={() => setExcludedTerms((current) => current.filter((item) => item !== term))}
                        >
                          {term}<XMarkIcon />
                        </button>
                      ))}
                    </div>
                  ) : <p className="analysis-keyword-manager-empty">제외 중인 단어가 없습니다.</p>}
                </section>
              </>
            )}

            <footer>
              <button type="button" className="analysis-keyword-cancel" onClick={closeManager}>취소</button>
              <button type="button" className="analysis-keyword-save" onClick={() => void save()} disabled={loading || saving}>
                {saving ? "저장 중…" : "저장하고 결과 갱신"}
              </button>
            </footer>
          </div>
        </div>
      )}
    </section>
  );
}
