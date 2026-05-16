const API_BASE_RAW = import.meta.env.VITE_API_BASE || 'http://localhost:8000';
const API_PREFIX = '/v1';
const API_BASE = `${API_BASE_RAW.replace(/\/$/, '')}${API_PREFIX}`;

async function readJson(response) {
  const text = await response.text();
  return text ? JSON.parse(text) : {};
}

async function buildApiError(response) {
  let detail = `Request failed with status ${response.status}`;
  try {
    const payload = await readJson(response);
    if (payload?.detail) {
      detail = payload.detail;
    }
  } catch {
    // Ignore parse errors and keep generic detail.
  }
  const error = new Error(detail);
  error.status = response.status;
  return error;
}

function normalizeOptionalString(value) {
  if (typeof value !== 'string') {
    return '';
  }
  return value.trim();
}

function buildIngestPayload(input) {
  if (input && typeof input === 'object' && !Array.isArray(input)) {
    const repoPath = normalizeOptionalString(input.repo_path ?? input.repoPath);
    const repoName = normalizeOptionalString(input.repo_name ?? input.repoName);
    const payload = {};

    if (repoPath) {
      payload.repo_path = repoPath;
    }

    if (repoName) {
      payload.repo_name = repoName;
    }

    return payload;
  }

  const repoName = normalizeOptionalString(input);
  return repoName ? { repo_name: repoName } : {};
}

function getIngestRequestLabel(input) {
  if (input && typeof input === 'object' && !Array.isArray(input)) {
    return (
      normalizeOptionalString(input.repo_name ?? input.repoName) ||
      normalizeOptionalString(input.repo_path ?? input.repoPath) ||
      'Unknown repository'
    );
  }

  return normalizeOptionalString(input) || 'Unknown repository';
}

export const Api = {
  async fetchRepos() {
    const response = await fetch(`${API_BASE}/repos`);
    if (!response.ok) {
      throw await buildApiError(response);
    }
    const data = await readJson(response);
    return data.repos || [];
  },

  async createConversation(repoId) {
    const response = await fetch(`${API_BASE}/conversations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo_name: repoId })
    });
    if (!response.ok) {
      throw await buildApiError(response);
    }
    return readJson(response);
  },

  async listConversations(repoId = null, limit = 50, offset = 0) {
    const params = new URLSearchParams({ limit: limit.toString(), offset: offset.toString() });
    if (repoId) {
      params.append('repo_name', repoId);
    }
    const response = await fetch(`${API_BASE}/conversations?${params}`);
    if (!response.ok) {
      throw await buildApiError(response);
    }
    return readJson(response);
  },

  async getConversationMessages(conversationId, limit = 200) {
    const response = await fetch(`${API_BASE}/conversations/${conversationId}/messages?limit=${limit}`);
    if (!response.ok) {
      throw await buildApiError(response);
    }
    return readJson(response);
  },

  async sendChatMessage(conversationId, message, onEvent) {
    const response = await fetch(`${API_BASE}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversation_id: conversationId,
        message: message
      })
    });
    if (!response.ok) {
      throw await buildApiError(response);
    }
    if (!response.body) {
      throw new Error('Chat response did not include a stream body');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let accumulated = '';
    let buffer = '';
    let streamError = null;

    const handleLine = (line) => {
      const trimmed = line.trim();
      if (!trimmed) {
        return;
      }

      const event = JSON.parse(trimmed);
      onEvent?.(event);

      if (event.type === 'content') {
        accumulated += event.delta || '';
      }

      if (event.type === 'error') {
        streamError = event;
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }

      const chunk = decoder.decode(value, { stream: true });
      buffer += chunk;

      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      lines.forEach(handleLine);
    }

    buffer += decoder.decode();
    if (buffer.trim()) {
      handleLine(buffer);
    }

    if (streamError) {
      const error = new Error(streamError.message || 'Chat stream failed');
      error.event = streamError;
      throw error;
    }

    return accumulated;
  },

  async deleteConversation(conversationId) {
    const response = await fetch(`${API_BASE}/conversations/${conversationId}`, {
      method: 'DELETE',
    });
    if (!response.ok) {
      throw await buildApiError(response);
    }
    return readJson(response);
  },

  async deleteRepo(repoName, deleteFiles = false) {
    const params = new URLSearchParams({ delete_files: String(deleteFiles) });
    const response = await fetch(
      `${API_BASE}/repos/${encodeURIComponent(repoName)}?${params}`,
      { method: 'DELETE' },
    );
    if (!response.ok) {
      throw await buildApiError(response);
    }
    return readJson(response);
  },

  async browseLocalRepos(path = null) {
    const params = new URLSearchParams();
    if (path) {
      params.append('path', path);
    }

    const query = params.toString();
    const response = await fetch(`${API_BASE}/local/repos/browse${query ? `?${query}` : ''}`);
    if (!response.ok) {
      throw await buildApiError(response);
    }
    return readJson(response);
  },

  async ingestRepo(repoRequest) {
    const response = await fetch(`${API_BASE}/ingest`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildIngestPayload(repoRequest))
    });
    if (!response.ok) {
      throw await buildApiError(response);
    }
    return readJson(response);
  },

  async ingestRepos(repoRequests) {
    const repos = Array.isArray(repoRequests) ? repoRequests : [repoRequests];
    const results = [];

    for (let i = 0; i < repos.length; i += 1) {
      const repoRequest = repos[i];
      const requestLabel = getIngestRequestLabel(repoRequest);
      try {
        const data = await this.ingestRepo(repoRequest);
        results.push({
          repo_name: data.repo_name || requestLabel,
          ok: true,
          data,
        });
      } catch (error) {
        results.push({
          repo_name: requestLabel,
          ok: false,
          error: error.message || 'Failed to start ingestion',
          status: error.status || null,
        });
        if (error.status === 409) {
          for (let j = i + 1; j < repos.length; j += 1) {
            results.push({
              repo_name: getIngestRequestLabel(repos[j]),
              ok: false,
              error: error.message || 'An ingestion job is already in progress.',
              status: 409,
            });
          }
          break;
        }
      }
    }

    return results;
  },

  async getJobStatus(jobId) {
    const response = await fetch(`${API_BASE}/jobs/${encodeURIComponent(jobId)}`);
    if (!response.ok) {
      throw await buildApiError(response);
    }
    return readJson(response);
  },

  async listJobs(filters = {}) {
    const params = new URLSearchParams();
    if (filters.status) params.append('status', filters.status);
    if (filters.repo_name) params.append('repo_name', filters.repo_name);
    params.append('limit', (filters.limit || 50).toString());
    params.append('skip', (filters.skip || 0).toString());

    const response = await fetch(`${API_BASE}/jobs?${params}`);
    if (!response.ok) {
      throw await buildApiError(response);
    }
    return readJson(response);
  },

  async deleteJob(jobId) {
    const response = await fetch(`${API_BASE}/jobs/${jobId}`, {
      method: 'DELETE',
    });
    if (!response.ok) {
      throw await buildApiError(response);
    }
    return readJson(response);
  },
};
