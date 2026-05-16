import { useState, useEffect, useRef } from 'react';
import { Api } from '../api/Api';

const TERMINAL_JOB_STATUSES = new Set(['completed', 'failed', 'cancelled']);
const CHAT_MESSAGE_TYPE = 'chat_message';
const PROGRESS_EVENT_TYPE = 'progress_event';

function normalizeJob(job) {
  return {
    jobId: job.job_id || job.jobId,
    job_id: job.job_id || job.jobId,
    repo_name: job.repo_name,
    status: job.status,
    current_stage: job.current_stage,
    error: job.error || null,
    created_at: job.created_at,
    updated_at: job.updated_at,
    stages: job.stages || {},
    isLocalOnly: job.isLocalOnly || false,
  };
}

function normalizeMetadata(metadata) {
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) {
    return {};
  }

  return metadata;
}

function normalizeConversationMessage(message) {
  return {
    role: message.role || 'assistant',
    content: message.content || '',
    message_type: message.message_type || CHAT_MESSAGE_TYPE,
    stage: message.stage ?? null,
    status: message.status ?? null,
    metadata: normalizeMetadata(message.metadata),
    created_at: message.created_at || new Date().toISOString(),
    client_id: message.client_id || null,
    run_id: message.run_id || null,
  };
}

function getProgressIdentity(message) {
  if (message.stage === 'reading_file') {
    const filePath = normalizeMetadata(message.metadata).file_path;
    return `${message.stage}:${filePath || message.created_at}`;
  }

  return message.stage || message.created_at;
}

function upsertProgressMessage(messages, nextMessage) {
  if (nextMessage.message_type !== PROGRESS_EVENT_TYPE) {
    return [...messages, nextMessage];
  }

  const next = [...messages];
  const nextIdentity = getProgressIdentity(nextMessage);
  if (nextMessage.run_id) {
    const existingIndex = next.findIndex((message) => (
      message.message_type === PROGRESS_EVENT_TYPE &&
      message.run_id === nextMessage.run_id &&
      getProgressIdentity(message) === nextIdentity
    ));

    if (existingIndex !== -1) {
      next[existingIndex] = nextMessage;
      return next;
    }

    const assistantIndex = next.findIndex((message) => (
      message.message_type === CHAT_MESSAGE_TYPE &&
      message.role === 'assistant' &&
      message.run_id === nextMessage.run_id
    ));

    if (assistantIndex !== -1) {
      next.splice(assistantIndex, 0, nextMessage);
      return next;
    }
  }

  let currentRunStart = next.length;
  while (currentRunStart > 0 && next[currentRunStart - 1]?.message_type === PROGRESS_EVENT_TYPE) {
    currentRunStart -= 1;
  }

  const existingIndex = next.findIndex((message, index) => {
    if (index < currentRunStart) {
      return false;
    }

    if (message.message_type !== PROGRESS_EVENT_TYPE) {
      return false;
    }

    if (getProgressIdentity(message) !== nextIdentity) {
      return false;
    }

    return true;
  });

  if (existingIndex === -1) {
    next.push(nextMessage);
    return next;
  }

  next[existingIndex] = nextMessage;
  return next;
}

function normalizeConversationMessages(messages) {
  return (messages || []).reduce((acc, rawMessage) => {
    const message = normalizeConversationMessage(rawMessage);
    if (message.message_type === PROGRESS_EVENT_TYPE) {
      return upsertProgressMessage(acc, message);
    }

    acc.push(message);
    return acc;
  }, []);
}

export function useRepoChat() {
  const [repos, setRepos] = useState([]);
  const [selectedRepo, setSelectedRepo] = useState(null);
  const [expandedRepos, setExpandedRepos] = useState({});

  const [conversations, setConversations] = useState([]);
  const [selectedConversationId, setSelectedConversationId] = useState(null);
  const [chatMessages, setChatMessages] = useState([]);
  const [isChatStreaming, setIsChatStreaming] = useState(false);

  const [ingestModalOpen, setIngestModalOpen] = useState(false);
  const [ingestionJobs, setIngestionJobs] = useState([]);

  const contentRef = useRef(null);
  const pollingIntervals = useRef({});
  const messageCounter = useRef(0);

  const withClientId = (message) => ({
    ...normalizeConversationMessage(message),
    client_id: message.client_id || `message-${Date.now()}-${messageCounter.current++}`,
  });

  const createChatMessage = (role, content, createdAt = new Date().toISOString()) => withClientId({
    role,
    content,
    message_type: CHAT_MESSAGE_TYPE,
    created_at: createdAt,
  });

  const createProgressEventMessage = ({
    content = '',
    stage = null,
    status = null,
    metadata = {},
    created_at,
    run_id = null,
  }) => withClientId({
    role: 'system',
    content,
    message_type: PROGRESS_EVENT_TYPE,
    stage,
    status,
    metadata,
    created_at: created_at || new Date().toISOString(),
    run_id,
  });

  const clearPollingForJob = (jobId) => {
    if (pollingIntervals.current[jobId]) {
      clearInterval(pollingIntervals.current[jobId]);
      delete pollingIntervals.current[jobId];
    }
  };

  const pollJobStatus = async (jobId) => {
    try {
      const status = await Api.getJobStatus(jobId);
      const normalized = normalizeJob(status);

      setIngestionJobs(prev => {
        const existing = prev.find(j => j.jobId === jobId);
        if (existing) {
          return prev.map(j => (j.jobId === jobId ? { ...j, ...normalized } : j));
        }
        return [...prev, normalized];
      });

      if (TERMINAL_JOB_STATUSES.has(normalized.status)) {
        clearPollingForJob(jobId);

        if (normalized.status === 'completed') {
          Api.fetchRepos()
            .then(setRepos)
            .catch(err => console.error('Failed to refresh repos:', err));
        }
      }
    } catch (err) {
      console.error('Failed to poll job status:', err);
      if (err.status === 404) {
        clearPollingForJob(jobId);
      }
    }
  };

  const startPollingJob = (jobId) => {
    if (pollingIntervals.current[jobId]) {
      return;
    }

    pollJobStatus(jobId);
    pollingIntervals.current[jobId] = setInterval(() => {
      pollJobStatus(jobId);
    }, 10000);
  };

  useEffect(() => {
    Api.fetchRepos()
      .then(setRepos)
      .catch(err => console.error('Failed to fetch repos:', err));
  }, []);

  useEffect(() => {
    Api.listJobs({ limit: 100 })
      .then(response => {
        if (!response.jobs || response.jobs.length === 0) {
          return;
        }

        const jobs = response.jobs.map(normalizeJob);
        setIngestionJobs(jobs);

        jobs.forEach(job => {
          if (['running', 'pending', 'queued'].includes(job.status)) {
            startPollingJob(job.jobId);
          }
        });
      })
      .catch(err => console.error('Failed to fetch existing jobs:', err));
  }, []);

  useEffect(() => {
    if (selectedRepo) {
      Api.listConversations(selectedRepo)
        .then(setConversations)
        .catch(err => console.error('Failed to fetch conversations:', err));
    } else {
      setConversations([]);
    }
  }, [selectedRepo]);

  useEffect(() => {
    return () => {
      Object.values(pollingIntervals.current).forEach(interval => {
        if (interval) clearInterval(interval);
      });
    };
  }, []);

  const handleNewConversation = async () => {
    if (!selectedRepo) return;

    try {
      const newConv = await Api.createConversation(selectedRepo);
      setConversations(prev => [newConv, ...prev]);
      setSelectedConversationId(newConv.conversation_id);
      setChatMessages([]);
    } catch (err) {
      console.error('Failed to create conversation:', err);
    }
  };

  const handleSelectConversation = async (conversationId) => {
    setSelectedConversationId(conversationId);

    try {
      const response = await Api.getConversationMessages(conversationId);
      setChatMessages(normalizeConversationMessages(response.messages || []).map(withClientId));
    } catch (err) {
      console.error('Failed to load messages:', err);
    }
  };

  const handleSendMessage = async (message) => {
    if (!selectedConversationId || isChatStreaming) return;

    const userMessage = createChatMessage('user', message);
    setChatMessages(prev => [...prev, userMessage]);

    setIsChatStreaming(true);
    const responseRunId = `run-${Date.now()}-${messageCounter.current++}`;
    const assistantMessageId = `message-${Date.now()}-${messageCounter.current++}`;
    let hasSeenStreamError = false;

    try {
      await Api.sendChatMessage(
        selectedConversationId,
        message,
        (event) => {
          if (event.type === 'progress') {
            setChatMessages(prev => upsertProgressMessage(
              prev,
              createProgressEventMessage({
                content: event.message || '',
                stage: event.stage ?? null,
                status: event.status ?? null,
                metadata: event.metadata,
                created_at: event.created_at,
                run_id: responseRunId,
              })
            ));
            return;
          }

          if (event.type === 'content') {
            const delta = event.delta || '';
            if (!delta) {
              return;
            }

            setChatMessages(prev => {
              const existingIndex = prev.findIndex(msg => msg.client_id === assistantMessageId);
              if (existingIndex === -1) {
                return [
                  ...prev,
                  {
                    ...createChatMessage('assistant', delta, event.created_at),
                    client_id: assistantMessageId,
                    run_id: responseRunId,
                  },
                ];
              }

              const next = [...prev];
              const existing = next[existingIndex];
              next[existingIndex] = {
                ...existing,
                content: `${existing.content}${delta}`,
              };
              return next;
            });
            return;
          }

          if (event.type === 'error') {
            hasSeenStreamError = true;
            setChatMessages(prev => upsertProgressMessage(
              prev,
              createProgressEventMessage({
                content: event.message || 'Chat stream failed',
                status: 'failed',
                created_at: event.created_at,
                run_id: responseRunId,
              })
            ));
          }
        }
      );
    } catch (err) {
      console.error('Failed to send message:', err);
      if (!hasSeenStreamError) {
        setChatMessages(prev => upsertProgressMessage(
          prev,
          createProgressEventMessage({
            content: err.message || 'Failed to send message',
            status: 'failed',
            run_id: responseRunId,
          })
        ));
      }
    } finally {
      setIsChatStreaming(false);
    }
  };

  const handleDeleteConversation = async (conversationId) => {
    try {
      await Api.deleteConversation(conversationId);

      setConversations(prev => prev.filter(c => c.conversation_id !== conversationId));

      if (selectedConversationId === conversationId) {
        setSelectedConversationId(null);
        setChatMessages([]);
      }
    } catch (err) {
      console.error('Failed to delete conversation:', err);
    }
  };

  const handleDeleteRepo = async (repoName, deleteFiles) => {
    try {
      await Api.deleteRepo(repoName, deleteFiles);

      setRepos(prev => prev.filter(r => r !== repoName));
      setIngestionJobs(prev => {
        const remainingJobs = [];

        prev.forEach((job) => {
          if (job.repo_name === repoName) {
            clearPollingForJob(job.jobId);
            return;
          }
          remainingJobs.push(job);
        });

        return remainingJobs;
      });
      setExpandedRepos(prev => {
        if (!(repoName in prev)) return prev;
        const next = { ...prev };
        delete next[repoName];
        return next;
      });

      if (selectedRepo === repoName) {
        setSelectedRepo(null);
        setConversations([]);
        setSelectedConversationId(null);
        setChatMessages([]);
      }
    } catch (err) {
      console.error('Failed to delete repo:', err);
      alert(err.message || 'Failed to delete repository');
    }
  };

  const handleOpenIngestModal = () => {
    setIngestModalOpen(true);
  };

  const handleCloseIngestModal = () => {
    setIngestModalOpen(false);
  };

  const registerNewJob = (jobPayload) => {
    const job = normalizeJob(jobPayload);
    if (!job.jobId) {
      throw new Error('Ingestion response missing job_id');
    }

    setIngestionJobs(prev => {
      const existing = prev.find(j => j.jobId === job.jobId);
      if (existing) {
        return prev.map(j => (j.jobId === job.jobId ? { ...j, ...job } : j));
      }
      return [...prev, job];
    });

    startPollingJob(job.jobId);
  };

  const handleIngestRepo = async (repoRequest) => {
    try {
      const result = await Api.ingestRepo(repoRequest);
      setIngestModalOpen(false);
      registerNewJob(result);
    } catch (err) {
      console.error('Failed to ingest repo:', err);
      throw err;
    }
  };

  const handleIngestRepos = async (repoRequests) => {
    try {
      const results = await Api.ingestRepos(repoRequests);
      const successful = results.filter(result => result.ok);
      const failed = results.filter(result => !result.ok);

      if (successful.length === 0) {
        const firstError = failed[0];
        const error = new Error(firstError?.error || 'Failed to start ingestion');
        error.status = firstError?.status || null;
        throw error;
      }

      setIngestModalOpen(false);

      const failedEntries = [];
      let localFailureIndex = 0;

      results.forEach((result) => {
        if (result.ok) {
          registerNewJob(result.data);
          return;
        }

        localFailureIndex += 1;
        failedEntries.push(normalizeJob({
          job_id: `local-failure-${Date.now()}-${localFailureIndex}`,
          repo_name: result.repo_name,
          status: 'failed',
          current_stage: 'queued',
          error: result.error || 'Failed to start ingestion',
          isLocalOnly: true,
        }));
      });

      if (failedEntries.length > 0) {
        setIngestionJobs(prev => [...prev, ...failedEntries]);
      }
    } catch (err) {
      console.error('Failed to ingest repos:', err);
      throw err;
    }
  };

  const handleRemoveJob = (jobId) => {
    setIngestionJobs(prev => prev.filter(j => j.jobId !== jobId));
    clearPollingForJob(jobId);
  };

  const handleDeleteJob = async (jobId) => {
    const job = ingestionJobs.find(j => j.jobId === jobId);
    if (job?.isLocalOnly) {
      handleRemoveJob(jobId);
      return;
    }

    try {
      await Api.deleteJob(jobId);
      handleRemoveJob(jobId);
    } catch (err) {
      console.error('Failed to delete job:', err);
      const errorMsg = err.message || 'Failed to delete job';
      if (errorMsg.toLowerCase().includes('running')) {
        alert('Job is running. Retry delete after it finishes.');
      } else {
        alert(errorMsg);
      }
    }
  };

  const toggleRepo = (repo) => {
    setExpandedRepos(prev => ({ ...prev, [repo]: !prev[repo] }));
  };

  const selectRepo = (repo) => {
    setSelectedRepo(repo);
    setSelectedConversationId(null);
    setChatMessages([]);
  };

  return {
    repos,
    selectedRepo,
    expandedRepos,
    conversations,
    selectedConversationId,
    chatMessages,
    isChatStreaming,
    contentRef,
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
    handleIngestRepos,
    handleRemoveJob,
    handleDeleteJob,
  };
}
