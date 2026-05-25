import React, { useEffect, useState } from 'react';
import {
  ArrowUp,
  Check,
  ChevronRight,
  Folder,
  FolderGit2,
  FolderOpen,
  Loader2,
  RefreshCw,
  Tag,
  X,
} from 'lucide-react';
import { Api } from '../api/Api';

export default function IngestRepoModal({ isOpen, onClose, onIngest }) {
  const [selectedRepoPath, setSelectedRepoPath] = useState('');
  const [repoNameOverride, setRepoNameOverride] = useState('');
  const [browserData, setBrowserData] = useState(null);
  const [browserLoading, setBrowserLoading] = useState(false);
  const [browserError, setBrowserError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (isOpen) {
      setIsSubmitting(false);
      setError('');
      setBrowserError('');
    }
  }, [isOpen]);

  const loadBrowserPath = async (path = null) => {
    setBrowserLoading(true);
    setBrowserError('');

    try {
      const data = await Api.browseLocalRepos(path);
      setBrowserData(data);
    } catch (err) {
      setBrowserError(err.message || 'Failed to browse local folders');
    } finally {
      setBrowserLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      loadBrowserPath();
    }
  }, [isOpen]);

  const startIngestion = async (repoPath, repoName) => {
    const normalizedPath = repoPath.trim();

    if (!normalizedPath) {
      setError('Please select a repository');
      return;
    }

    await onIngest({
      repo_path: normalizedPath,
      ...(repoName ? { repo_name: repoName } : {}),
    });

    setSelectedRepoPath('');
    setRepoNameOverride('');
  };

  const handleSelectPath = (path) => {
    setSelectedRepoPath(current => (current.trim() === path ? '' : path));
    setError('');
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!selectedRepoPath.trim()) return;

    setIsSubmitting(true);
    setError('');

    try {
      const repoName = repoNameOverride.trim();
      await startIngestion(selectedRepoPath, repoName);
    } catch (err) {
      if (err?.status === 409) {
        setError(err.message || 'An ingestion job is already in progress.');
      } else {
        setError(err.message || 'Failed to start ingestion');
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  if (!isOpen) return null;

  const submitDisabled = !selectedRepoPath.trim() || isSubmitting;
  const selectedPath = selectedRepoPath.trim();
  const sortedEntries = [...(browserData?.entries || [])].sort((a, b) => {
    if (a.has_git !== b.has_git) {
      return a.has_git ? -1 : 1;
    }
    return a.name.localeCompare(b.name);
  });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/45 p-4 backdrop-blur-sm">
      <div className="surface-card flex max-h-[90vh] w-full max-w-xl flex-col rounded-2xl p-6">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <h3 className="text-lg font-semibold text-slate-900">Ingest Repository</h3>
          </div>
          <button
            onClick={onClose}
            className="rounded p-1 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600"
            disabled={isSubmitting}
          >
            <X size={20} />
          </button>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="mb-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2 text-sm font-semibold text-slate-800">
                  <FolderOpen size={15} />
                  <span>Browse Local Folders</span>
                </div>
                <div className="mt-1 truncate text-xs text-slate-500">
                  {browserData?.path || 'Configured roots'}
                </div>
              </div>
              <button
                type="button"
                onClick={() => loadBrowserPath(browserData?.path || null)}
                disabled={browserLoading || isSubmitting}
                className="rounded-lg border border-slate-200 bg-white p-1.5 text-slate-600 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
                title="Refresh"
              >
                <RefreshCw size={14} className={browserLoading ? 'animate-spin' : ''} />
              </button>
            </div>

            {browserError && (
              <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                {browserError}
              </div>
            )}

            {browserData?.roots?.length > 0 && (
              <div className="mb-3 flex flex-wrap gap-2">
                {browserData.roots.map((root) => (
                  <button
                    key={root.path}
                    type="button"
                    onClick={() => loadBrowserPath(root.path)}
                    disabled={browserLoading || isSubmitting}
                    className="inline-flex max-w-full items-center gap-1.5 rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1.5 text-xs font-medium text-slate-700 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
                    title={root.path}
                  >
                    <Folder size={13} />
                    <span className="truncate">{root.name}</span>
                  </button>
                ))}
              </div>
            )}

            <div className="max-h-80 overflow-y-auto rounded-lg border border-slate-200">
              {browserLoading ? (
                <div className="flex items-center justify-center gap-2 px-3 py-8 text-sm text-slate-500">
                  <Loader2 size={16} className="animate-spin" />
                  Loading folders
                </div>
              ) : (
                <>
                  {browserData?.parent_path && (
                    <button
                      type="button"
                      onClick={() => loadBrowserPath(browserData.parent_path)}
                      disabled={isSubmitting}
                      className="flex w-full items-center gap-2 border-b border-slate-100 px-3 py-2 text-left text-sm text-slate-700 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <ArrowUp size={15} />
                      Parent folder
                    </button>
                  )}

                  {sortedEntries.map((entry) => {
                    const isSelected = selectedPath === entry.path;
                    const EntryIcon = entry.has_git ? FolderGit2 : Folder;
                    return (
                      <div
                        key={entry.path}
                        className={`flex items-center gap-2 border-b border-slate-100 px-3 py-2 last:border-b-0 ${
                          isSelected ? 'bg-cyan-50' : 'bg-white'
                        }`}
                      >
                        <button
                          type="button"
                          onClick={() => loadBrowserPath(entry.path)}
                          disabled={browserLoading || isSubmitting}
                          className="flex min-w-0 flex-1 items-center gap-2 rounded-md text-left disabled:cursor-not-allowed disabled:opacity-50"
                          title={entry.path}
                        >
                          <EntryIcon size={15} className={entry.has_git ? 'text-emerald-600' : 'text-slate-500'} />
                          <span className="min-w-0 flex-1 truncate text-sm font-medium text-slate-800">
                            {entry.name}
                          </span>
                          {entry.has_git && (
                            <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-semibold text-emerald-700">
                              git
                            </span>
                          )}
                          <ChevronRight size={14} className="text-slate-400" />
                        </button>

                        {entry.has_git && (
                          <button
                            type="button"
                            onClick={() => handleSelectPath(entry.path)}
                            disabled={isSubmitting}
                            className={`rounded-lg border px-2 py-1 text-xs font-semibold transition disabled:cursor-not-allowed disabled:opacity-50 ${
                              isSelected
                                ? 'border-cyan-200 bg-cyan-100 text-cyan-800'
                                : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-100'
                            }`}
                          >
                            {isSelected ? 'Selected' : 'Select'}
                          </button>
                        )}
                      </div>
                    );
                  })}

                  {sortedEntries.length === 0 && !browserError && (
                    <div className="px-3 py-8 text-center text-sm text-slate-500">
                      No folders found.
                    </div>
                  )}
                </>
              )}
            </div>

            {selectedPath && (
              <div className="mt-3 flex items-start gap-1.5 rounded-lg border border-cyan-200 bg-cyan-50 px-3 py-2 text-xs text-cyan-800">
                  <Check size={13} className="mt-0.5 shrink-0" />
                  <span className="break-all">{selectedPath}</span>
              </div>
            )}

            <div className="mt-4">
              <label htmlFor="repoNameOverride" className="mb-2 block text-sm font-semibold text-slate-700">
                Display Key
              </label>
              <div className="relative">
                <Tag size={16} className="pointer-events-none absolute left-3 top-3 text-slate-400" />
                <input
                  id="repoNameOverride"
                  type="text"
                  value={repoNameOverride}
                  onChange={(e) => setRepoNameOverride(e.target.value)}
                  placeholder="Optional"
                  className="w-full rounded-xl border border-slate-300 bg-white py-2 pl-10 pr-3 text-sm text-slate-700 outline-none transition placeholder:text-slate-400 focus:border-cyan-500 focus:ring-2 focus:ring-cyan-200"
                  disabled={isSubmitting}
                />
              </div>
            </div>
          </div>

          {error && (
            <div className="mb-4 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              {error}
            </div>
          )}

          <div className="flex justify-end gap-3">
            <button
              type="button"
              onClick={onClose}
              disabled={isSubmitting}
              className="rounded-lg bg-slate-100 px-4 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-200 disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitDisabled}
              className="rounded-lg bg-cyan-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-cyan-700 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {isSubmitting ? 'Starting...' : 'Ingest Selected'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
