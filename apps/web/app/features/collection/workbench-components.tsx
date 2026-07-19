import {
  ArrowPathIcon,
  CheckCircleIcon,
  ClockIcon,
  ExclamationTriangleIcon,
  FolderIcon,
  MagnifyingGlassIcon,
  PlayIcon,
} from "@heroicons/react/24/outline";
import type { JobStatus } from "@monitube/contracts";
import type { FormEvent, ReactNode } from "react";
import { useState } from "react";

import {
  login,
  register,
  type ChannelSubscriberSnapshot,
  type SourceSummary,
} from "../../lib/api";
import {
  formatCount,
  formatKpiDate,
  sourceCollectionState,
  statusCopy,
} from "./workbench-model";

export const sourceTypeChoices = [
  { type: "channel" as const, label: "채널", detail: "업로드 동영상 기준", Icon: FolderIcon },
  { type: "keyword" as const, label: "키워드", detail: "검색 run별 발견 결과", Icon: MagnifyingGlassIcon },
];

export function SubscriberTrend({ samples }: { samples: ChannelSubscriberSnapshot[] }) {
  const visible = samples.filter((sample) => sample.subscriberCount !== undefined && !sample.hiddenSubscriberCount);
  if (visible.length < 2) return <p className="subscriber-trend-empty">구독자 수집 이력이 쌓이면 변동 추이를 보여드립니다.</p>;
  const values = visible.map((sample) => sample.subscriberCount ?? 0);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(1, max - min);
  const points = visible.map((sample, index) => `${(index / (visible.length - 1)) * 100},${40 - (((sample.subscriberCount ?? min) - min) / range) * 32}`).join(" ");
  const delta = values.at(-1)! - values[0];
  return (
    <section className="subscriber-trend" aria-label="구독자 수 변동 추이">
      <div><span>구독자 추이</span><strong className={delta > 0 ? "subscriber-trend-positive" : ""}>{delta > 0 ? "+" : ""}{formatCount(delta)}</strong></div>
      <svg viewBox="0 0 100 48" role="img" aria-label={`${formatKpiDate(visible[0].fetchedAt)}부터 ${formatKpiDate(visible.at(-1)?.fetchedAt)}까지 구독자 ${formatCount(values[0])}명에서 ${formatCount(values.at(-1))}명`} preserveAspectRatio="none"><polyline points={points} /></svg>
      <small>{formatKpiDate(visible[0].fetchedAt)} · {formatKpiDate(visible.at(-1)?.fetchedAt)}</small>
    </section>
  );
}

export function StatusPill({ job }: { job?: JobStatus | null }) {
  if (!job) return <span className="status-pill status-idle">수집 기록 없음</span>;
  const Icon = job.state === "completed"
    ? CheckCircleIcon
    : job.state === "failed" || job.state === "cancelled"
      ? ExclamationTriangleIcon
      : job.state === "waiting_quota" || job.state === "waiting_retry"
        ? ClockIcon
        : ArrowPathIcon;
  return (
    <span className={`status-pill status-${job.state}`}>
      <Icon aria-hidden="true" />
      {statusCopy(job)}
    </span>
  );
}

export function SourceCollectionState({ source }: { source: SourceSummary }) {
  const state = sourceCollectionState(source);
  return (
    <span className={`source-progress source-progress-state source-progress-state-${state.tone}`}>{state.label}</span>
  );
}

export function MetricCard({
  label,
  value,
  detail,
  icon,
  accent = false,
  failure = false,
  onClick,
}: {
  label: string;
  value: string;
  detail: string;
  icon: ReactNode;
  accent?: boolean;
  failure?: boolean;
  onClick?: () => void;
}) {
  const className = `${accent ? "metric-card metric-card-accent" : "metric-card"}${failure ? " metric-card-failure" : ""}`;
  const content = <><div className="metric-card-head"><span>{label}</span><span className="metric-icon" aria-hidden="true">{icon}</span></div><strong>{value}</strong><small>{detail}</small></>;
  return (
    onClick ? <button type="button" className={`${className} metric-card-button`} onClick={onClick}>{content}</button> : <article className={className}>{content}</article>
  );
}

export function LoginScreen({ onAuthenticated }: { onAuthenticated: (username: string) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [mode, setMode] = useState<"login" | "register">("login");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setIsSubmitting(true);
    setError(null);
    try {
      const user = mode === "login" ? await login(username, password) : await register(username, password);
      onAuthenticated(user.username);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "로그인할 수 없습니다.");
    } finally {
      setIsSubmitting(false);
    }
  };
  return <main className="login-page"><section className="login-card"><div className="brand-lockup"><span className="brand-mark"><PlayIcon /></span><span>monitube</span></div><p className="section-kicker">PRIVATE COLLECTION WORKSPACE</p><h1>{mode === "login" ? "로그인" : "계정 만들기"}</h1><p>아이디와 비밀번호만 저장합니다. 수집 데이터는 로그인한 계정별로 분리됩니다.</p><form onSubmit={submit}><label>아이디<input value={username} onChange={(event) => setUsername(event.target.value)} minLength={3} maxLength={32} pattern="[A-Za-z0-9_-]+" autoComplete="username" required /></label><label>비밀번호<input value={password} onChange={(event) => setPassword(event.target.value)} minLength={8} maxLength={256} type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} required /></label>{error && <p className="inline-error">{error}</p>}<button className="primary-action" type="submit" disabled={isSubmitting}>{isSubmitting ? "확인 중…" : mode === "login" ? "로그인" : "계정 생성"}</button></form><button className="login-mode-switch" type="button" onClick={() => { setMode((current) => current === "login" ? "register" : "login"); setError(null); }}>{mode === "login" ? "새 계정 만들기" : "이미 계정이 있습니다"}</button></section></main>;
}
