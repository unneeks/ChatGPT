import { Routes, Route, NavLink } from "react-router-dom";
import QueueView from "./views/QueueView";
import WorkspaceView from "./views/WorkspaceView";
import AuditView from "./views/AuditView";

export default function App() {
  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-gray-200 bg-white shadow-sm">
        <div className="mx-auto flex max-w-screen-xl items-center gap-8 px-6 py-3">
          <span className="text-lg font-bold tracking-tight text-brand-700">
            ReqSmith
            <span className="ml-2 text-xs font-normal text-gray-500">Reviewer Console</span>
          </span>
          <nav className="flex gap-6 text-sm font-medium">
            <NavLink
              to="/"
              end
              className={({ isActive }) =>
                isActive ? "text-brand-600 border-b-2 border-brand-500 pb-1" : "text-gray-600 hover:text-brand-600"
              }
            >
              Queue
            </NavLink>
            <NavLink
              to="/audit"
              className={({ isActive }) =>
                isActive ? "text-brand-600 border-b-2 border-brand-500 pb-1" : "text-gray-600 hover:text-brand-600"
              }
            >
              Audit Replay
            </NavLink>
          </nav>
        </div>
      </header>

      <main className="flex-1 mx-auto w-full max-w-screen-xl px-6 py-6">
        <Routes>
          <Route path="/" element={<QueueView />} />
          <Route path="/runs/:runId" element={<WorkspaceView />} />
          <Route path="/audit" element={<AuditView />} />
          <Route path="/audit/:runId" element={<AuditView />} />
        </Routes>
      </main>
    </div>
  );
}
