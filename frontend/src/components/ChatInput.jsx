import React, { useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, Check, ChevronRight, FolderTree, Loader2, Send } from 'lucide-react';

export default function ChatInput({
  onSend,
  sendDisabled = false,
  subdirOptions = [],
  isSubdirOptionsLoading = false,
}) {
  const [message, setMessage] = useState('');
  const [cursorIndex, setCursorIndex] = useState(0);
  const [isFocused, setIsFocused] = useState(false);
  const [activeSuggestionIndex, setActiveSuggestionIndex] = useState(0);
  const textareaRef = useRef(null);
  const optionIndex = useMemo(() => buildOptionIndex(subdirOptions), [subdirOptions]);
  const subdirMentions = useMemo(() => extractSubdirMentions(message), [message]);
  const invalidMentions = useMemo(
    () => subdirMentions.filter(path => !optionIndex.validPathSet.has(path)),
    [subdirMentions, optionIndex.validPathSet]
  );
  const activeMention = useMemo(
    () => (isFocused ? getActiveMention(message, cursorIndex) : null),
    [message, cursorIndex, isFocused]
  );
  const suggestions = useMemo(
    () => buildMentionSuggestions(activeMention?.query || '', optionIndex),
    [activeMention, optionIndex]
  );
  const showSuggestions = Boolean(
    activeMention &&
    (isSubdirOptionsLoading || suggestions.length > 0 || activeMention.query)
  );
  const hasMentionWhileLoading = isSubdirOptionsLoading && subdirMentions.length > 0;
  const canSubmit = Boolean(message.trim()) && !sendDisabled && invalidMentions.length === 0 && !hasMentionWhileLoading;

  useEffect(() => {
    setActiveSuggestionIndex(0);
  }, [activeMention?.query, suggestions.length]);

  const handleSubmit = (e) => {
    e.preventDefault();
    if (canSubmit) {
      onSend(message, { subdirContextPaths: subdirMentions });
      setMessage('');
      setCursorIndex(0);
    }
  };

  const updateCursorFromTextarea = (textarea) => {
    setCursorIndex(textarea.selectionStart ?? textarea.value.length);
  };

  const insertMentionPath = (path, keepOpen) => {
    if (!activeMention) {
      return;
    }

    const suffix = keepOpen ? '/' : ' ';
    const nextMessage = `${message.slice(0, activeMention.start)}@${path}${suffix}${message.slice(activeMention.end)}`;
    const nextCursorIndex = activeMention.start + path.length + 1 + suffix.length;

    setMessage(nextMessage);
    setCursorIndex(nextCursorIndex);
    setIsFocused(true);

    requestAnimationFrame(() => {
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(nextCursorIndex, nextCursorIndex);
    });
  };

  const handleSuggestionSelect = (suggestion) => {
    if (suggestion.kind === 'use' || !suggestion.hasChildren) {
      insertMentionPath(suggestion.path, false);
      return;
    }

    insertMentionPath(suggestion.path, true);
  };

  const handleKeyDown = (e) => {
    if (showSuggestions && suggestions.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActiveSuggestionIndex(index => (index + 1) % suggestions.length);
        return;
      }

      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActiveSuggestionIndex(index => (index - 1 + suggestions.length) % suggestions.length);
        return;
      }

      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSuggestionSelect(suggestions[activeSuggestionIndex]);
        return;
      }

      if (e.key === 'Tab') {
        e.preventDefault();
        handleSuggestionSelect(suggestions[activeSuggestionIndex]);
        return;
      }
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="border-t border-slate-200/80 bg-white/75 p-4 backdrop-blur-md sm:p-5">
      {showSuggestions ? (
        <div className="mb-2 w-full max-w-xl overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl">
          {isSubdirOptionsLoading ? (
            <div className="flex items-center gap-2 px-3 py-2.5 text-sm text-slate-500">
              <Loader2 size={15} className="animate-spin" />
              Loading paths
            </div>
          ) : suggestions.length > 0 ? (
            <div className="max-h-64 overflow-y-auto py-1">
              {suggestions.map((suggestion, index) => {
                const Icon = suggestion.kind === 'use' ? Check : FolderTree;
                return (
                  <button
                    key={`${suggestion.kind}-${suggestion.path}`}
                    type="button"
                    onMouseDown={(event) => {
                      event.preventDefault();
                      handleSuggestionSelect(suggestion);
                    }}
                    className={`flex w-full items-center gap-2 px-3 py-2 text-left transition ${
                      index === activeSuggestionIndex ? 'bg-cyan-50 text-cyan-900' : 'text-slate-700 hover:bg-slate-50'
                    }`}
                  >
                    <Icon size={15} className="flex-shrink-0" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-mono text-xs">@{suggestion.path}</span>
                      <span className="block text-[11px] text-slate-500">
                        {suggestion.fileCount.toLocaleString()} {suggestion.fileCount === 1 ? 'file' : 'files'}
                      </span>
                    </span>
                    {suggestion.kind !== 'use' && suggestion.hasChildren ? (
                      <ChevronRight size={14} className="flex-shrink-0 text-slate-400" />
                    ) : null}
                  </button>
                );
              })}
            </div>
          ) : (
            <div className="flex items-center gap-2 px-3 py-2.5 text-sm text-amber-700">
              <AlertTriangle size={15} />
              No matching paths
            </div>
          )}
        </div>
      ) : null}
      {subdirMentions.length > 0 || hasMentionWhileLoading ? (
        <div className="mb-2 flex flex-wrap gap-2">
          {subdirMentions.map(path => (
            <span
              key={path}
              className={`inline-flex max-w-full items-center gap-1 rounded-lg border px-2 py-1 font-mono text-xs ${
                optionIndex.validPathSet.has(path)
                  ? 'border-cyan-200 bg-cyan-50 text-cyan-800'
                  : 'border-amber-200 bg-amber-50 text-amber-800'
              }`}
              title={`@${path}`}
            >
              {optionIndex.validPathSet.has(path) ? (
                <FolderTree size={13} className="flex-shrink-0" />
              ) : (
                <AlertTriangle size={13} className="flex-shrink-0" />
              )}
              <span className="truncate">@{path}</span>
            </span>
          ))}
          {hasMentionWhileLoading ? (
            <span className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-slate-50 px-2 py-1 text-xs text-slate-500">
              <Loader2 size={13} className="animate-spin" />
              Loading paths
            </span>
          ) : null}
        </div>
      ) : null}
      <div className="flex gap-2">
        <textarea
          ref={textareaRef}
          value={message}
          onChange={(e) => {
            setMessage(e.target.value);
            updateCursorFromTextarea(e.target);
          }}
          onKeyDown={handleKeyDown}
          onKeyUp={(e) => updateCursorFromTextarea(e.currentTarget)}
          onClick={(e) => updateCursorFromTextarea(e.currentTarget)}
          onFocus={(e) => {
            setIsFocused(true);
            updateCursorFromTextarea(e.currentTarget);
          }}
          onBlur={() => setIsFocused(false)}
          placeholder="Ask about architecture, files, or implementation details..."
          className={`flex-1 resize-none rounded-xl border bg-white px-3 py-2.5 text-sm text-slate-700 outline-none transition focus:ring-2 ${
            invalidMentions.length > 0
              ? 'border-amber-300 focus:border-amber-500 focus:ring-amber-200'
              : 'border-slate-300 focus:border-cyan-500 focus:ring-cyan-200'
          }`}
          rows={3}
        />
        <button
          type="submit"
          disabled={!canSubmit}
          className="flex items-center justify-center rounded-xl bg-cyan-600 px-4 text-white transition hover:bg-cyan-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          aria-label="Send message"
          title={sendDisabled ? 'Wait for the current response to finish' : 'Send message'}
        >
          <Send size={18} />
        </button>
      </div>
    </form>
  );
}

const SUBDIR_MENTION_PATTERN = /(^|[^A-Za-z0-9_./-])@((?:\.\/)?[A-Za-z0-9][A-Za-z0-9._/-]*)/g;
const TRAILING_MENTION_PUNCTUATION = new Set(['.', ',', ';', ':', '!', '?', ')', ']', '}', '"', "'"]);
const MENTION_BOUNDARY_PATTERN = /[A-Za-z0-9_./-]/;
const MENTION_QUERY_PATTERN = /^[A-Za-z0-9._/-]*$/;

function normalizeSubdirPath(path) {
  let cleaned = path.trim();
  if (cleaned.startsWith('@')) {
    cleaned = cleaned.slice(1);
  }
  while (cleaned && TRAILING_MENTION_PUNCTUATION.has(cleaned[cleaned.length - 1])) {
    cleaned = cleaned.slice(0, -1);
  }
  cleaned = cleaned.replace(/\\/g, '/');
  if (!cleaned || cleaned.startsWith('/') || cleaned.startsWith('~')) {
    return '';
  }
  while (cleaned.startsWith('./')) {
    cleaned = cleaned.slice(2);
  }

  const parts = cleaned.split('/').filter(part => part && part !== '.');
  if (parts.length === 0 || parts.some(part => part === '..')) {
    return '';
  }
  return parts.join('/');
}

function extractSubdirMentions(value) {
  const paths = [];
  const seen = new Set();
  let match;

  SUBDIR_MENTION_PATTERN.lastIndex = 0;
  while ((match = SUBDIR_MENTION_PATTERN.exec(value)) !== null) {
    const normalized = normalizeSubdirPath(match[2] || '');
    if (normalized && !seen.has(normalized)) {
      paths.push(normalized);
      seen.add(normalized);
    }
  }

  return paths;
}

function buildOptionIndex(options) {
  const pathToOption = new Map();
  const childrenByParent = new Map();

  (options || []).forEach((option) => {
    const path = normalizeSubdirPath(option?.path || '');
    if (!path) {
      return;
    }
    const fileCount = Number(option.file_count ?? option.fileCount ?? 0);
    pathToOption.set(path, {
      path,
      fileCount: Number.isFinite(fileCount) ? fileCount : 0,
    });

    const parts = path.split('/');
    for (let index = 0; index < parts.length; index += 1) {
      const parent = parts.slice(0, index).join('/');
      const childPath = parts.slice(0, index + 1).join('/');
      const segment = parts[index];
      if (!childrenByParent.has(parent)) {
        childrenByParent.set(parent, new Map());
      }
      childrenByParent.get(parent).set(segment, childPath);
    }
  });

  return {
    pathToOption,
    validPathSet: new Set(pathToOption.keys()),
    childrenByParent,
  };
}

function getActiveMention(value, cursorIndex) {
  const beforeCursor = value.slice(0, cursorIndex);
  const atIndex = beforeCursor.lastIndexOf('@');
  if (atIndex === -1) {
    return null;
  }

  const beforeAt = beforeCursor[atIndex - 1] || '';
  if (beforeAt && MENTION_BOUNDARY_PATTERN.test(beforeAt)) {
    return null;
  }

  const query = beforeCursor.slice(atIndex + 1);
  if (/\s/.test(query) || !MENTION_QUERY_PATTERN.test(query)) {
    return null;
  }

  return {
    start: atIndex,
    end: cursorIndex,
    query,
  };
}

function getQueryParts(query) {
  let cleaned = query.trim().replace(/\\/g, '/');
  while (cleaned.startsWith('./')) {
    cleaned = cleaned.slice(2);
  }
  if (cleaned.startsWith('/') || cleaned.startsWith('~') || cleaned.split('/').some(part => part === '..')) {
    return null;
  }

  const withoutTrailingSlash = cleaned.endsWith('/') ? cleaned.slice(0, -1) : cleaned;
  const slashIndex = withoutTrailingSlash.lastIndexOf('/');
  if (cleaned.endsWith('/')) {
    return {
      exactPath: withoutTrailingSlash,
      parentPath: withoutTrailingSlash,
      partial: '',
    };
  }

  if (slashIndex === -1) {
    return {
      exactPath: withoutTrailingSlash,
      parentPath: '',
      partial: withoutTrailingSlash,
    };
  }

  return {
    exactPath: withoutTrailingSlash,
    parentPath: withoutTrailingSlash.slice(0, slashIndex),
    partial: withoutTrailingSlash.slice(slashIndex + 1),
  };
}

function buildMentionSuggestions(query, optionIndex) {
  const queryParts = getQueryParts(query);
  if (!queryParts) {
    return [];
  }

  const suggestions = [];
  if (queryParts.exactPath && optionIndex.pathToOption.has(queryParts.exactPath) && !query.endsWith('/')) {
    const option = optionIndex.pathToOption.get(queryParts.exactPath);
    suggestions.push({
      kind: 'use',
      path: option.path,
      fileCount: option.fileCount,
      hasChildren: Boolean(optionIndex.childrenByParent.get(option.path)?.size),
    });
  }

  const childMap = optionIndex.childrenByParent.get(queryParts.parentPath) || new Map();
  const children = Array.from(childMap.entries())
    .filter(([segment]) => segment.toLowerCase().startsWith(queryParts.partial.toLowerCase()))
    .map(([, path]) => {
      const option = optionIndex.pathToOption.get(path) || { path, fileCount: 0 };
      return {
        kind: 'browse',
        path,
        fileCount: option.fileCount,
        hasChildren: Boolean(optionIndex.childrenByParent.get(path)?.size),
      };
    });

  return [...suggestions, ...children];
}
