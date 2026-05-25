import React, { useState } from 'react';
import {
  ChevronRight,
  ChevronDown,
  Folder,
  Search,
  Trash2,
  X,
  Plus,
  CircleDashed,
  AlertTriangle,
  CheckCircle2,
  Loader2,
  ListChecks,
} from 'lucide-react';
import ConversationList from './ConversationList';

export default function Sidebar({
  repos,
  allRepoCount,
  availableRepos,
  searchTerm,
  onSearchChange,
  selectedRepo,
  expandedRepos,
  toggleRepo,
  selectRepo,
  conversations,
  selectedConversationId,
  onSelectConversation,
  onNewConversation,
  onDeleteConversation,
  onDeleteRepo,
  onOpenIngestModal,
  ingestionJobs,
  onOpenRepoJobs,
}) {
  const [deleteModalOpen, setDeleteModalOpen] = useState(false);
  const [repoToDelete, setRepoToDelete] = useState(null);
  const [deleteFiles, setDeleteFiles] = useState(false);
  const availableRepoSet = new Set(availableRepos);

  const handleDeleteClick = (e, repo) => {
    e.stopPropagation();
    setRepoToDelete(repo);
    setDeleteModalOpen(true);
    setDeleteFiles(false);
  };

  const confirmDelete = () => {
    if (repoToDelete) {
      onDeleteRepo(repoToDelete, deleteFiles);
      setDeleteModalOpen(false);
      setRepoToDelete(null);
      setDeleteFiles(false);
    }
  };

  const cancelDelete = () => {
    setDeleteModalOpen(false);
    setRepoToDelete(null);
    setDeleteFiles(false);
  };

  const handleRepoSelect = (repo) => {
    if (!availableRepoSet.has(repo)) {
      return;
    }

    selectRepo(repo);
    if (!expandedRepos[repo]) {
      toggleRepo(repo);
    }
  };

  const getRepoStatus = (repo) => {
    const repoJobs = ingestionJobs
      .filter(job => job.repo_name === repo)
      .sort((a, b) => {
        const aTime = a.updated_at ? new Date(a.updated_at).getTime() : 0;
        const bTime = b.updated_at ? new Date(b.updated_at).getTime() : 0;
        return bTime - aTime;
      });

    if (repoJobs.length === 0) {
      return { label: 'Ready', tone: 'ready', icon: CheckCircle2 };
    }

    const latest = repoJobs[0];
    if (latest.status === 'running' || latest.status === 'queued' || latest.status === 'pending') {
      return { label: 'Ingesting', tone: 'ingesting', icon: Loader2 };
    }

    if (latest.status === 'failed' || latest.status === 'cancelled') {
      return { label: 'Attention', tone: 'attention', icon: AlertTriangle };
    }

    return { label: 'Ready', tone: 'ready', icon: CheckCircle2 };
  };

  const statusStyles = {
    ready: 'text-emerald-700 bg-emerald-50 border-emerald-200',
    ingesting: 'text-cyan-700 bg-cyan-50 border-cyan-200',
    attention: 'text-amber-700 bg-amber-50 border-amber-200',
    unknown: 'text-slate-600 bg-slate-100 border-slate-200',
  };

  return (
    <>
      <div className="surface-card fade-in-up w-full max-w-[340px] overflow-y-auto rounded-3xl">
        <div className="p-4">
          <div className="mb-4">
            <div className="mb-3 flex items-center justify-between">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-700">
                <Folder size={16} />
                <span>Repositories</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-medium text-slate-600">
                  {allRepoCount} total
                </span>
                <button
                  onClick={onOpenIngestModal}
                  className="rounded-lg border border-slate-200 bg-white p-1.5 text-slate-600 transition hover:bg-slate-100"
                  title="Ingest repository"
                >
                  <Plus size={16} className="text-gray-600" />
                </button>
              </div>
            </div>

            <label className="relative block">
              <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3 text-slate-400">
                <Search size={15} />
              </div>
              <input
                type="text"
                placeholder="Search repositories..."
                value={searchTerm}
                onChange={(e) => onSearchChange(e.target.value)}
                className="w-full rounded-2xl border border-slate-200 bg-white py-3 pl-10 pr-4 text-sm text-slate-700 outline-none transition placeholder:text-slate-400 focus:border-slate-400 focus:ring-2 focus:ring-slate-200"
              />
            </label>

            {searchTerm ? (
              <p className="mt-2 px-1 text-xs text-slate-500">
                Showing {repos.length} match{repos.length === 1 ? '' : 'es'} for{' '}
                <span className="font-medium text-slate-700">"{searchTerm}"</span>
              </p>
            ) : null}
          </div>

          <div className="space-y-2">
            {repos.map(repo => {
              const status = getRepoStatus(repo);
              const StatusIcon = status.icon || CircleDashed;
              const repoJobCount = ingestionJobs.filter(job => job.repo_name === repo).length;
              const isAvailable = availableRepoSet.has(repo);
              return (
                <div key={repo} className="rounded-xl border border-slate-200 bg-white p-2">
                  <div
                    className={`group flex items-center gap-2 rounded-lg px-2 py-2 transition ${
                      selectedRepo === repo && isAvailable
                        ? 'bg-cyan-50 ring-1 ring-cyan-200'
                        : 'hover:bg-slate-100'
                    }`}
                  >
                    <button
                      onClick={() => toggleRepo(repo)}
                      className="rounded p-0.5 text-slate-500 transition hover:bg-slate-200"
                      aria-label={expandedRepos[repo] ? 'Collapse repository' : 'Expand repository'}
                    >
                      {expandedRepos[repo] ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </button>
                    <button
                      onClick={() => handleRepoSelect(repo)}
                      className={`flex min-w-0 flex-1 items-center gap-2 rounded-md text-left ${
                        isAvailable ? 'cursor-pointer' : 'cursor-not-allowed'
                      }`}
                    >
                      <span className={`truncate text-sm font-medium ${isAvailable ? 'text-slate-800' : 'text-slate-500'}`}>
                        {repo.split('/').pop()}
                      </span>
                    </button>
                    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold ${statusStyles[status.tone] || statusStyles.unknown}`}>
                      <StatusIcon size={12} className={status.tone === 'ingesting' ? 'animate-spin' : ''} />
                      {status.label}
                    </span>
                    <button
                      onClick={(e) => handleDeleteClick(e, repo)}
                      disabled={!isAvailable}
                      className="rounded p-1 text-red-600 opacity-0 transition group-hover:opacity-100 hover:bg-red-50"
                      title="Delete repository"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>

                  {expandedRepos[repo] && (
                    <div className="ml-3 mt-1">
                      {!isAvailable && (
                        <p className="mt-1 px-2 text-xs text-slate-400">
                          Available for chat after ingestion completes.
                        </p>
                      )}
                      {repoJobCount > 0 && (
                        <button
                          onClick={() => onOpenRepoJobs(repo)}
                          className="mt-1 inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs font-medium text-slate-600 transition hover:bg-slate-100"
                        >
                          <ListChecks size={12} />
                          View jobs ({repoJobCount})
                        </button>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {repos.length === 0 && (
            <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 px-4 py-6 text-center text-sm text-slate-500">
              {searchTerm ? 'No repositories match your search.' : 'No repositories yet. Ingest your first one.'}
            </div>
          )}

          {selectedRepo && (
            <ConversationList
              conversations={conversations}
              selectedConversationId={selectedConversationId}
              onSelectConversation={onSelectConversation}
              onNewConversation={onNewConversation}
              onDeleteConversation={onDeleteConversation}
            />
          )}
        </div>
      </div>

      {deleteModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/45 p-4 backdrop-blur-sm">
          <div className="surface-card w-full max-w-md rounded-2xl p-6">
            <div className="mb-4 flex items-center justify-between">
              <h3 className="text-lg font-semibold text-slate-900">Delete Repository</h3>
              <button onClick={cancelDelete} className="rounded p-1 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600">
                <X size={20} />
              </button>
            </div>

            <p className="mb-4 text-sm text-slate-600">
              Delete <strong>{repoToDelete}</strong> from the catalog?
            </p>

            <label className="mb-6 flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                checked={deleteFiles}
                onChange={(e) => setDeleteFiles(e.target.checked)}
                className="h-4 w-4 rounded border-slate-300 text-cyan-600 focus:ring-2 focus:ring-cyan-300"
              />
              <span className="text-sm text-slate-700">
                Also delete files from disk
              </span>
            </label>

            <div className="flex justify-end gap-3">
              <button
                onClick={cancelDelete}
                className="rounded-lg bg-slate-100 px-4 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-200"
              >
                Cancel
              </button>
              <button
                onClick={confirmDelete}
                className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-red-700"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
