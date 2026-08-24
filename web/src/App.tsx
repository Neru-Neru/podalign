import { useEffect, useState } from "react";
import ProjectList from "./components/ProjectList";
import Pipeline from "./components/Pipeline";

function parseHash(): string | null {
  const m = window.location.hash.match(/^#\/p\/([a-z0-9-]+)$/);
  return m ? m[1] : null;
}

export default function App() {
  const [projectId, setProjectId] = useState<string | null>(parseHash());

  useEffect(() => {
    const onHash = () => setProjectId(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const open = (id: string) => { window.location.hash = `#/p/${id}`; };
  const back = () => { window.location.hash = ""; };

  return projectId
    ? <Pipeline projectId={projectId} onBack={back} />
    : <ProjectList onOpen={open} />;
}
