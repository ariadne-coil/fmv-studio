"use client";

import { useEffect, useState } from "react";

import { GlassCard } from "@/components/ui/GlassCard";
import { FolderOpen, Plus, Video, Loader2, Clock3, Trash2 } from "lucide-react";
import { motion } from "framer-motion";
import { useRouter } from "next/navigation";
import { api, ProjectSummary } from "@/lib/api";

export default function Home() {
  const router = useRouter();

  const [isLoading, setIsLoading] = useState(false);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [isLoadingProjects, setIsLoadingProjects] = useState(true);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [deletingProjectId, setDeletingProjectId] = useState<string | null>(null);

  const loadProjects = async () => {
    try {
      setIsLoadingProjects(true);
      const savedProjects = await api.listProjects();
      setProjects(savedProjects);
      setProjectsError(null);
    } catch (error) {
      setProjectsError(error instanceof Error ? error.message : "Failed to load saved projects.");
    } finally {
      setIsLoadingProjects(false);
    }
  };

  useEffect(() => {
    void loadProjects();
  }, []);

  const createNewProject = async () => {
    try {
      setIsLoading(true);
      const newId = `proj_${Date.now()}`;
      await api.createProject(newId, "Untitled Project");
      router.push(`/studio/${newId}`);
    } catch (e) {
      console.error(e);
      alert("Failed to create project on backend");
    } finally {
      setIsLoading(false);
    }
  };

  const openProject = (projectId: string) => {
    if (deletingProjectId === projectId) return;
    router.push(`/studio/${projectId}`);
  };

  const deleteProject = async (project: ProjectSummary) => {
    if (deletingProjectId) return;

    const confirmed = window.confirm(`Delete "${project.name || "Untitled Project"}"? This cannot be undone.`);
    if (!confirmed) return;

    try {
      setDeletingProjectId(project.project_id);
      await api.deleteProject(project.project_id);
      setProjects((currentProjects) => currentProjects.filter((item) => item.project_id !== project.project_id));
      setProjectsError(null);
    } catch (error) {
      alert(error instanceof Error ? error.message : "Failed to delete project.");
    } finally {
      setDeletingProjectId(null);
    }
  };

  const formatUpdatedAt = (isoDate: string) => {
    const date = new Date(isoDate);
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(date);
  };

  return (
    <main className="min-h-screen flex flex-col items-center justify-center p-8 relative overflow-hidden">
      {/* Background glow effects */}
      <div className="absolute top-1/4 left-1/4 w-96 h-96 bg-primary/20 rounded-full blur-[128px] pointer-events-none" />
      <div className="absolute bottom-1/4 right-1/4 w-96 h-96 bg-purple-500/10 rounded-full blur-[128px] pointer-events-none" />

      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="text-center mb-16 z-10"
      >
        <div className="flex items-center justify-center mb-6">
          <div className="p-4 rounded-2xl bg-gradient-to-br from-primary to-purple-600 shadow-lg shadow-primary/25">
            <Video className="w-12 h-12 text-white" />
          </div>
        </div>
        <h1 className="text-5xl font-bold mb-4 tracking-tight text-transparent bg-clip-text bg-gradient-to-r from-white to-white/70">
          FMV Studio
        </h1>
        <p className="text-xl text-surface-border max-w-2xl mx-auto">
          Agentic Music Video Production Powered by Google ADK
        </p>
      </motion.div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-8 max-w-5xl w-full z-10">
        <GlassCard
          hoverable
          onClick={createNewProject}
          className="flex flex-col items-center justify-center text-center p-12"
        >
          <div className="w-16 h-16 rounded-full bg-primary/10 flex items-center justify-center mb-6 group-hover:bg-primary/20 transition-colors">
            {isLoading ? <Loader2 className="w-8 h-8 text-primary animate-spin" /> : <Plus className="w-8 h-8 text-primary" />}
          </div>
          <h2 className="text-2xl font-semibold mb-2 text-white">New Project</h2>
          <p className="text-surface-border">Start a new agentic video generation pipeline</p>
        </GlassCard>

        <GlassCard
          className="flex flex-col p-8 min-h-[28rem]"
        >
          <div className="flex items-start justify-between gap-4 mb-6">
            <div>
              <div className="w-14 h-14 rounded-full bg-surface-hover/50 flex items-center justify-center mb-4">
                <FolderOpen className="w-7 h-7 text-white/70" />
              </div>
              <h2 className="text-2xl font-semibold mb-2 text-white">Saved Projects</h2>
              <p className="text-surface-border">Open an existing workspace and continue editing it</p>
            </div>
            <button
              onClick={() => void loadProjects()}
              className="shrink-0 rounded-lg border border-surface-border px-3 py-2 text-xs font-medium text-white/70 transition-colors hover:bg-surface-hover hover:text-white"
            >
              Refresh
            </button>
          </div>

          <div className="flex-1 min-h-0 rounded-2xl border border-surface-border bg-background/40 overflow-hidden">
            {isLoadingProjects ? (
              <div className="h-full flex items-center justify-center text-surface-border">
                <Loader2 className="w-5 h-5 animate-spin mr-2" />
                Loading saved projects...
              </div>
            ) : projectsError ? (
              <div className="h-full flex items-center justify-center p-6 text-center text-sm text-rose-300">
                {projectsError}
              </div>
            ) : projects.length === 0 ? (
              <div className="h-full flex flex-col items-center justify-center p-6 text-center">
                <FolderOpen className="w-10 h-10 text-white/20 mb-3" />
                <p className="text-white/80 font-medium">No saved projects yet</p>
                <p className="text-sm text-surface-border mt-1">Create a project and use Save Project in the studio to keep working on it later.</p>
              </div>
            ) : (
              <div className="max-h-[28rem] overflow-y-auto">
                {projects.map((project) => (
                  <div
                    key={project.project_id}
                    role="button"
                    tabIndex={0}
                    onClick={() => openProject(project.project_id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        openProject(project.project_id);
                      }
                    }}
                    className="w-full px-5 py-4 border-b border-surface-border/70 text-left transition-colors hover:bg-surface-hover/40 last:border-b-0 cursor-pointer"
                  >
                    <div className="flex items-center justify-between gap-4">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 min-w-0">
                          <div className="text-base font-semibold text-white truncate">{project.name || "Untitled Project"}</div>
                          <button
                            type="button"
                            onClick={(event) => {
                              event.stopPropagation();
                              void deleteProject(project);
                            }}
                            disabled={deletingProjectId === project.project_id}
                            aria-label={`Delete ${project.name || "Untitled Project"}`}
                            className="shrink-0 rounded-md p-1.5 text-white/40 transition-colors hover:bg-rose-500/10 hover:text-rose-300 disabled:cursor-not-allowed disabled:opacity-60"
                          >
                            {deletingProjectId === project.project_id ? (
                              <Loader2 className="w-4 h-4 animate-spin" />
                            ) : (
                              <Trash2 className="w-4 h-4" />
                            )}
                          </button>
                        </div>
                        <div className="text-xs text-surface-border mt-1 truncate">{project.project_id}</div>
                      </div>
                      <div className="text-[11px] uppercase tracking-[0.18em] text-primary shrink-0">
                        {project.current_stage.replaceAll("_", " ")}
                      </div>
                    </div>
                    <div className="mt-3 flex items-center gap-2 text-xs text-surface-border">
                      <Clock3 className="w-3.5 h-3.5" />
                      Updated {formatUpdatedAt(project.updated_at)}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </GlassCard>
      </div>
    </main>
  );
}
