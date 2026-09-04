import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { Lookup } from "./pages/Lookup";
import { Decisions } from "./pages/Decisions";
import { Actions } from "./pages/Actions";
import { ChangeLog } from "./pages/ChangeLog";
import { AskNba } from "./pages/AskNba";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/lookup" replace />} />
          <Route path="/lookup" element={<Lookup />} />
          <Route path="/decisions" element={<Decisions />} />
          <Route path="/actions" element={<Actions />} />
          <Route path="/change-log" element={<ChangeLog />} />
          <Route path="/ask" element={<AskNba />} />
          <Route path="*" element={<Navigate to="/lookup" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
