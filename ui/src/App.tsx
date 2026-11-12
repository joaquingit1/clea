import { Stopped } from "./components/Stopped";
import { useEffect, useState } from "react";
import { usePoll, type Board as B, type ChatSummary, type Thread } from "./api";
import { ActivityFeed } from "./components/ActivityFeed";
import { Board } from "./components/Board";
import { ChatThread } from "./components/Chat";
import { Palette } from "./components/Palette";
import { Settings } from "./components/Settings";
import { Sidebar } from "./components/Sidebar";
import { Threads } from "./components/Threads";
import { ThreadView } from "./components/ThreadView";
import { PrView } from "./components/PrView";
import { Sky } from "./components/Sky";
import { Onboarding } from "./components/setup/Onboarding";
import { NotRunning, Startup } from "./components/setup/shell";
import { desktop } from "./desktop";
import { useLang } from "./i18n";
import { go, href, swap, useRoute, type Route } from "./router";
import { useBoot, type SetupState } from "./setup";

/** What the window shows depends on where setup stands: the startup mark until the worker has answered, "Clea is not
 *  running" when it cannot be reached, the onboarding until setup is complete, and from then on the app. */
export default function App() {
  useLang(); // a change of language renders every screen again
  const boot = useBoot();
  if (boot.phase === "loading") return <Startup />;
  if (boot.phase === "down") return <NotRunning />;
  if (boot.state && !boot.state.complete) return <Onboarding state={boot.state} />;
  return <Console setup={boot.state} />;
}

function Console({ setup }: { setup: SetupState | null }) {
  const route = useRoute();
  const { data: board, refresh: refreshBoard } = usePoll<B>("/api/board", 10000);
  const { data: threads } = usePoll<Thread[]>("/api/threads", 10000);
  const { data: chats } = usePoll<ChatSummary[]>("/api/chats", 5000);
  const [palette, setPalette] = useState(false);

  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPalette((p) => !p);
      } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "j") {
        e.preventDefault();
        go({ page: "chat", conv: null });
      } else if ((e.metaKey || e.ctrlKey) && e.key === ",") {
        e.preventDefault();
        go({ page: "settings" });
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  useEffect(() => {
    const n = (threads ?? []).filter((t) => t.phase === "you").length;
    document.title = n ? `(${n}) Clea` : "Clea";
    desktop?.setBadge(n);
  }, [threads]);

  useEffect(() => {
    desktop?.onCommand((c) => {
      if (c === "palette") setPalette((p) => !p);
      else if (c === "new-chat") go({ page: "chat", conv: null });
      else if (c === "board") go({ page: "board" });
      else if (c === "threads") go({ page: "threads" });
      else if (c === "activity") go({ page: "activity" });
      else if (c === "settings") go({ page: "settings" });
    });
  }, []);

  // setup is over: its address (a bookmark, or the last step's when Clea started) leads to the board
  useEffect(() => {
    if (route.page === "setup") swap({ page: "board" });
  }, [route.page]);

  const ts = threads ?? [];
  const ownUrls = new Set((board?.prs ?? []).map((p) => p.url.replace(/\/$/, "")));
  const cs = chats ?? [];
  const page = route.page === "setup" ? "board" : route.page;
  return (
    <div className="flex h-full overflow-hidden">
      <Sidebar route={route} board={board} threads={ts} chats={cs} onPalette={() => setPalette(true)} />
      <main className="app-main relative isolate flex min-w-0 flex-1 flex-col bg-bg">
        {/* the sky, always, except on a case or a PR */}
        <Sky hidden={page === "thread" || page === "pr"} />
        {desktop && !["thread", "threads", "pr"].includes(page) ? <div data-drag className="relative h-[46px] shrink-0" /> : null}
        {board?.health.stopped ? <Stopped stop={board.health.stopped} onResumed={refreshBoard} /> : null}
        <div key={pageKey(route)} className="page-in relative min-h-0 flex-1">
        {page === "board" ? <Board board={board} threads={ts} /> : null}
        {route.page === "threads" ? <Threads threads={ts} chats={cs} /> : null}
        {route.page === "thread" ? <ThreadView key={route.id} id={route.id} chats={cs} own={ownUrls} ownPrs={board?.prs} peek={ts.find((t) => t.id === route.id)} /> : null}
        {route.page === "pr" ? <PrView key={`${route.repo}#${route.n}`} repo={route.repo} n={route.n} board={board} /> : null}
        {route.page === "chat" ? (
          <div className="flex h-full flex-col">
            <ChatThread key={route.conv ?? "new"} conv={route.conv} onConv={(c) => go({ page: "chat", conv: c })} />
          </div>
        ) : null}
        {route.page === "activity" ? <ActivityFeed /> : null}
        {route.page === "settings" ? <Settings state={setup} /> : null}
        </div>
      </main>
      <Palette open={palette} onClose={() => setPalette(false)} threads={ts} chats={cs} />
    </div>
  );
}

/** One key per screen, so moving between screens plays the entrance once (a poll refresh never does). */
function pageKey(r: Route): string {
  return r.page === "chat" ? "chat" : r.page === "setup" ? href({ page: "board" }) : href(r);
}
