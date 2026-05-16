import React, { useMemo, useState } from 'react';
import Sidebar from './components/Sidebar';
import ChatView from './components/ChatView';
import IngestRepoModal from './components/IngestRepoModal';
import RepoJobsModal from './components/RepoJobsModal';
import { useRepoChat } from './hooks/useRepoChat';

export default function App() {
  const {
    repos,
    selectedRepo,
    expandedRepos,
    conversations,
    selectedConversationId,
    chatMessages,
    isChatStreaming,
    ingestModalOpen,
    ingestionJobs,
    toggleRepo,
    selectRepo,
    handleNewConversation,
    handleSelectConversation,
    handleSendMessage,
    handleDeleteConversation,
    handleDeleteRepo,
    handleOpenIngestModal,
    handleCloseIngestModal,
    handleIngestRepo,
    handleRemoveJob,
    handleDeleteJob,
  } = useRepoChat();

  const [repoSearchTerm, setRepoSearchTerm] = useState('');
  const [jobsModalRepo, setJobsModalRepo] = useState(null);

  const catalogRepos = useMemo(() => {
    const seen = new Set();
    const orderedRepos = [];

    repos.forEach((repo) => {
      if (!seen.has(repo)) {
        seen.add(repo);
        orderedRepos.push(repo);
      }
    });

    ingestionJobs.forEach((job) => {
      if (job.repo_name && !seen.has(job.repo_name)) {
        seen.add(job.repo_name);
        orderedRepos.push(job.repo_name);
      }
    });

    return orderedRepos;
  }, [repos, ingestionJobs]);

  const filteredRepos = useMemo(() => {
    if (!repoSearchTerm.trim()) return catalogRepos;
    const query = repoSearchTerm.trim().toLowerCase();
    return catalogRepos.filter((repo) => repo.toLowerCase().includes(query));
  }, [repoSearchTerm, catalogRepos]);

  return (
    <div className="h-screen flex flex-col overflow-hidden px-2 py-2 sm:px-4 sm:py-4">
      <div className="flex flex-1 overflow-hidden gap-3">
        <Sidebar
          repos={filteredRepos}
          allRepoCount={catalogRepos.length}
          availableRepos={repos}
          searchTerm={repoSearchTerm}
          onSearchChange={setRepoSearchTerm}
          selectedRepo={selectedRepo}
          expandedRepos={expandedRepos}
          toggleRepo={toggleRepo}
          selectRepo={selectRepo}
          conversations={conversations}
          selectedConversationId={selectedConversationId}
          onSelectConversation={handleSelectConversation}
          onNewConversation={handleNewConversation}
          onDeleteConversation={handleDeleteConversation}
          onDeleteRepo={handleDeleteRepo}
          onOpenIngestModal={handleOpenIngestModal}
          ingestionJobs={ingestionJobs}
          onOpenRepoJobs={setJobsModalRepo}
        />

        <div className="surface-card fade-in-up flex-1 flex flex-col overflow-hidden rounded-3xl">
          {selectedConversationId ? (
            <ChatView
              messages={chatMessages}
              onSendMessage={handleSendMessage}
              isStreaming={isChatStreaming}
              selectedRepo={selectedRepo}
            />
          ) : (
            <div className="flex-1 flex items-center justify-center px-4">
              <div className="text-center max-w-md">
                <p className="text-2xl font-semibold text-slate-800 mb-2">Select a repository to begin</p>
                <p className="text-sm text-slate-500">
                  Choose a repo from the catalog, then open or create a conversation.
                </p>
              </div>
            </div>
          )}
        </div>
      </div>

      <IngestRepoModal
        isOpen={ingestModalOpen}
        onClose={handleCloseIngestModal}
        onIngest={handleIngestRepo}
      />

      <RepoJobsModal
        isOpen={Boolean(jobsModalRepo)}
        repoName={jobsModalRepo}
        jobs={ingestionJobs}
        onClose={() => setJobsModalRepo(null)}
        onRemoveJob={handleRemoveJob}
        onDeleteJob={handleDeleteJob}
      />
    </div>
  );
}
