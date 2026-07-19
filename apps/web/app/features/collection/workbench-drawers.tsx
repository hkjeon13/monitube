import { ChevronRightIcon, InformationCircleIcon, XMarkIcon } from "@heroicons/react/24/outline";
import type { KeywordSourceConfig } from "@monitube/contracts";
import type { Dispatch, FormEvent, RefObject, SetStateAction } from "react";

import { sourceTypeChoices } from "./workbench-components";
import type { CollectionPreferences, FormState } from "./workbench-model";

type SettingsDrawerProps = {
  drawerRef: RefObject<HTMLElement | null>;
  username: string;
  draft: CollectionPreferences;
  setDraft: Dispatch<SetStateAction<CollectionPreferences>>;
  onClose: () => void;
  onSave: (event: FormEvent<HTMLFormElement>) => void;
  onReset: () => void;
};

export function SettingsDrawer({ drawerRef, username, draft, setDraft, onClose, onSave, onReset }: SettingsDrawerProps) {
  return (
    <div className="drawer-layer">
      <div className="drawer-backdrop" aria-hidden="true" onClick={onClose} />
      <aside ref={drawerRef} className="settings-drawer" role="dialog" aria-modal="true" aria-labelledby="settings-drawer-title" aria-describedby="settings-drawer-description" tabIndex={-1}>
        <div className="drawer-heading">
          <div>
            <p className="section-kicker">PERSONAL DEFAULTS</p>
            <h2 id="settings-drawer-title">기본 설정</h2>
            <p id="settings-drawer-description">{username} 계정에서 새 수집 대상을 추가할 때 사용할 기본값입니다.</p>
          </div>
          <button className="icon-button" type="button" aria-label="기본 설정 창 닫기" data-drawer-initial-focus onClick={onClose}><XMarkIcon aria-hidden="true" /></button>
        </div>
        <form className="settings-form" onSubmit={onSave}>
          <section className="settings-section" aria-labelledby="settings-collection-title">
            <div><p className="section-kicker">NEW COLLECTION</p><h3 id="settings-collection-title">새 수집 기본값</h3></div>
            <div className="drawer-field-grid">
              <label className="drawer-field drawer-field-wide">
                <span>기본 수집 대상</span>
                <select value={draft.defaultSourceType} onChange={(event) => setDraft((current) => ({ ...current, defaultSourceType: event.target.value as CollectionPreferences["defaultSourceType"] }))}>
                  <option value="channel">채널</option>
                  <option value="keyword">키워드</option>
                </select>
                <small>수집 대상 추가 창을 열 때 먼저 선택할 유형입니다.</small>
              </label>
              <label className="toggle-field drawer-field-wide">
                <input type="checkbox" checked={draft.includeComments} onChange={(event) => setDraft((current) => ({ ...current, includeComments: event.target.checked }))} />
                <span className="toggle-visual" aria-hidden="true" />
                <span><strong>공개 댓글 포함</strong><small>새 채널·키워드 수집에서 공개 댓글을 기본으로 함께 수집합니다.</small></span>
              </label>
            </div>
          </section>
          <section className="settings-section" aria-labelledby="settings-keyword-title">
            <div><p className="section-kicker">KEYWORD SEARCH</p><h3 id="settings-keyword-title">키워드 검색 기본값</h3></div>
            <div className="drawer-field-grid">
              <label className="drawer-field drawer-field-wide">
                <span>정렬</span>
                <select value={draft.order} onChange={(event) => setDraft((current) => ({ ...current, order: event.target.value as CollectionPreferences["order"] }))}>
                  <option value="date">최신순</option>
                  <option value="relevance">관련도순</option>
                  <option value="viewCount">조회수순</option>
                </select>
              </label>
              <label className="drawer-field">
                <span>검색 언어</span>
                <input value={draft.relevanceLanguage} onChange={(event) => setDraft((current) => ({ ...current, relevanceLanguage: event.target.value }))} placeholder="ko" minLength={2} maxLength={10} pattern="[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,6})?" autoComplete="off" />
                <small>예: ko, en</small>
              </label>
              <label className="drawer-field">
                <span>지역 코드</span>
                <input value={draft.regionCode} onChange={(event) => setDraft((current) => ({ ...current, regionCode: event.target.value }))} placeholder="KR" minLength={2} maxLength={2} pattern="[A-Za-z]{2}" autoComplete="off" />
                <small>ISO 국가 코드, 예: KR, US</small>
              </label>
            </div>
          </section>
          <p className="settings-storage-note"><InformationCircleIcon aria-hidden="true" />이 설정은 현재 브라우저에 계정별로 저장되며 API 키 같은 운영 비밀값은 포함하지 않습니다.</p>
          <div className="drawer-footer-action settings-footer-action">
            <button className="secondary-action settings-reset-action" type="button" onClick={onReset}>설정 초기화</button>
            <button className="primary-action" type="submit">설정 저장</button>
          </div>
        </form>
      </aside>
    </div>
  );
}

type CollectionDrawerProps = {
  drawerRef: RefObject<HTMLElement | null>;
  form: FormState;
  isStarting: boolean;
  onClose: () => void;
  onSubmit: () => void;
  onUpdate: <K extends keyof FormState>(key: K, value: FormState[K]) => void;
};

export function CollectionDrawer({ drawerRef, form, isStarting, onClose, onSubmit, onUpdate }: CollectionDrawerProps) {
  return (
    <div className="drawer-layer">
      <div className="drawer-backdrop" aria-hidden="true" onClick={onClose} />
      <aside ref={drawerRef} className="collection-drawer" role="dialog" aria-modal="true" aria-labelledby="collection-drawer-title" tabIndex={-1}>
        <div className="drawer-heading">
          <div><p className="section-kicker">COLLECTION TARGET</p><h2 id="collection-drawer-title">수집 대상 추가</h2><p>같은 대상은 하나로 관리하고, 수집 결과를 함께 최신화합니다.</p></div>
          <button className="icon-button" type="button" aria-label="수집 대상 추가 창 닫기" data-drawer-initial-focus onClick={onClose}><XMarkIcon aria-hidden="true" /></button>
        </div>

        <form className="collection-form" onSubmit={(event) => { event.preventDefault(); onSubmit(); }}>
          <fieldset className="source-type-group">
            <legend>수집 대상</legend>
            <div className="source-type-tabs" role="tablist" aria-label="수집 대상 유형">
              {sourceTypeChoices.map(({ type, label, detail, Icon }) => (
                <button key={type} type="button" className={form.sourceType === type ? "source-type-choice source-type-choice-active" : "source-type-choice"} onClick={() => onUpdate("sourceType", type)} role="tab" aria-selected={form.sourceType === type}>
                  <Icon aria-hidden="true" />
                  <span><strong>{label}</strong><small>{detail}</small></span>
                </button>
              ))}
            </div>
          </fieldset>

          {form.sourceType === "channel" ? (
            <label className="drawer-field drawer-field-wide">
              <span>채널 URL, @handle 또는 채널 ID</span>
              <input value={form.channelInput} onChange={(event) => onUpdate("channelInput", event.target.value)} placeholder="예: @GoogleDevelopers 또는 @우정잉" autoComplete="off" />
              <small>한글·유니코드 핸들도 지원합니다. 채널 전체 업로드는 업로드 재생목록을 기준으로 수집합니다.</small>
            </label>
          ) : (
            <div className="drawer-field-grid">
              <label className="drawer-field drawer-field-wide"><span>검색 키워드</span><input value={form.keyword} onChange={(event) => onUpdate("keyword", event.target.value)} placeholder="예: 생성형 AI 교육" autoComplete="off" /></label>
              <label className="drawer-field"><span>시작일</span><input type="date" value={form.publishedAfter} onChange={(event) => onUpdate("publishedAfter", event.target.value)} /></label>
              <label className="drawer-field"><span>종료일</span><input type="date" value={form.publishedBefore} onChange={(event) => onUpdate("publishedBefore", event.target.value)} /></label>
              <label className="drawer-field"><span>정렬</span><select value={form.order} onChange={(event) => onUpdate("order", event.target.value as KeywordSourceConfig["order"])}><option value="date">최신순</option><option value="relevance">관련성순</option><option value="viewCount">조회수순</option></select></label>
              <label className="drawer-field"><span>관련 언어</span><input value={form.relevanceLanguage} maxLength={8} onChange={(event) => onUpdate("relevanceLanguage", event.target.value)} placeholder="ko" /></label>
              <label className="drawer-field"><span>지역</span><input value={form.regionCode} maxLength={2} onChange={(event) => onUpdate("regionCode", event.target.value)} placeholder="KR" /></label>
            </div>
          )}

          <section className="drawer-scope" aria-labelledby="scope-title">
            <div><p className="section-kicker">SCOPE</p><h3 id="scope-title">수집 범위</h3><p>{form.sourceType === "channel" ? "채널은 현재 공개된 전체 업로드를 수집합니다." : "선택한 대상의 공개 메타데이터를 수집합니다."} quota가 소진되면 1~3시간 간격으로 자동 재시도합니다.</p></div>
            <div className="scope-controls">
              <label className="toggle-field"><input type="checkbox" checked={form.includeComments} onChange={(event) => onUpdate("includeComments", event.target.checked)} /><span className="toggle-visual" aria-hidden="true" /><span><strong>공개 댓글 전체 수집</strong><small>댓글이 비활성화된 영상은 부분 경고로 남습니다.</small></span></label>
            </div>
          </section>

          <div className="drawer-footer-action">
            <button className="secondary-action" type="button" onClick={onClose}>취소</button>
            <button className="primary-action drawer-start-action" type="submit" disabled={isStarting}>{isStarting ? "요청을 연결하는 중…" : "수집 요청 보내기"}<ChevronRightIcon aria-hidden="true" /></button>
          </div>
        </form>
      </aside>
    </div>
  );
}
