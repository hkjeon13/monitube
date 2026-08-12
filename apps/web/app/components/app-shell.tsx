"use client";

import {
  Bars3Icon,
  ChartBarSquareIcon,
  Cog6ToothIcon,
  FolderIcon,
  HomeIcon,
  PlayIcon,
  QueueListIcon,
  Squares2X2Icon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import Link from "next/link";
import type { MouseEvent, ReactNode } from "react";
import { useState } from "react";

import type { WorkspacePage } from "../features/collection/workbench-model";

type AppShellProps = {
  page: WorkspacePage;
  settingsOpen: boolean;
  onOpenSettings: (event: MouseEvent<HTMLButtonElement>) => void;
  children: ReactNode;
};

const navigation = [
  { id: "explore" as const, label: "Explore", href: "/", Icon: Squares2X2Icon },
  { id: "overview" as const, label: "Channels", href: "/channels", Icon: HomeIcon },
  { id: "sources" as const, label: "Sources", href: "/sources", Icon: FolderIcon },
  { id: "jobs" as const, label: "Status", href: "/jobs", Icon: QueueListIcon },
  { id: "analysis" as const, label: "Analysis", href: "/analysis", Icon: ChartBarSquareIcon },
];

const mobileNavigation = navigation.filter(({ id }) => id !== "jobs");

function isCurrent(page: WorkspacePage, id: (typeof navigation)[number]["id"]) {
  if (page === "keywords") return id === "sources";
  return page === id;
}

export function AppShell({ page, settingsOpen, onOpenSettings, children }: AppShellProps) {
  const [moreOpen, setMoreOpen] = useState(false);

  return (
    <div className={`app-shell page-${page}`}>
      <a className="skip-link" href="#workspace-main">본문으로 건너뛰기</a>

      <aside className="sidebar" aria-label="Monitube 탐색">
        <Link className="brand-lockup" href="/" aria-label="Monitube Explore">
          <span className="brand-mark" aria-hidden="true"><PlayIcon /></span>
          <span>monitube</span>
        </Link>

        <nav className="sidebar-nav" aria-label="주요 메뉴">
          {navigation.map(({ id, label, href, Icon }) => (
            <Link
              key={id}
              className={isCurrent(page, id) ? "nav-item nav-item-active" : "nav-item"}
              aria-current={isCurrent(page, id) ? "page" : undefined}
              href={href}
            >
              <Icon aria-hidden="true" />
              <span>{label}</span>
            </Link>
          ))}
        </nav>

        <button
          className="sidebar-settings"
          type="button"
          onClick={onOpenSettings}
          aria-label="기본 설정 열기"
          aria-haspopup="dialog"
          aria-expanded={settingsOpen}
        >
          <Cog6ToothIcon aria-hidden="true" />
          <span>Settings</span>
        </button>
      </aside>

      <main className="dashboard-main" id="workspace-main" tabIndex={-1}>
        {children}
      </main>

      <nav className="mobile-bottom-nav" aria-label="모바일 주요 메뉴">
        {mobileNavigation.map(({ id, label, href, Icon }) => (
          <Link
            key={id}
            className={isCurrent(page, id) ? "mobile-nav-item mobile-nav-item-active" : "mobile-nav-item"}
            aria-current={isCurrent(page, id) ? "page" : undefined}
            href={href}
            onClick={() => setMoreOpen(false)}
          >
            <Icon aria-hidden="true" />
            <span>{label}</span>
          </Link>
        ))}
        <button
          className={moreOpen || page === "jobs" ? "mobile-nav-item mobile-nav-item-active" : "mobile-nav-item"}
          type="button"
          onClick={() => setMoreOpen((current) => !current)}
          aria-expanded={moreOpen}
          aria-controls="mobile-more-menu"
        >
          {moreOpen ? <XMarkIcon aria-hidden="true" /> : <Bars3Icon aria-hidden="true" />}
          <span>More</span>
        </button>
      </nav>

      {moreOpen && (
        <div className="mobile-more-layer" id="mobile-more-menu">
          <button className="mobile-more-backdrop" type="button" aria-label="더보기 메뉴 닫기" onClick={() => setMoreOpen(false)} />
          <section className="mobile-more-sheet" aria-label="추가 메뉴">
            <div>
              <strong>Workspace</strong>
              <button type="button" aria-label="더보기 메뉴 닫기" onClick={() => setMoreOpen(false)}><XMarkIcon aria-hidden="true" /></button>
            </div>
            <Link className={page === "jobs" ? "mobile-more-action mobile-more-action-active" : "mobile-more-action"} href="/jobs" onClick={() => setMoreOpen(false)}>
              <QueueListIcon aria-hidden="true" />
              <span><strong>Status</strong><small>수집 작업과 실패 기록</small></span>
            </Link>
            <button
              className="mobile-more-action"
              type="button"
              onClick={(event) => {
                setMoreOpen(false);
                onOpenSettings(event);
              }}
            >
              <Cog6ToothIcon aria-hidden="true" />
              <span><strong>Settings</strong><small>개인 수집 기본값</small></span>
            </button>
          </section>
        </div>
      )}
    </div>
  );
}
