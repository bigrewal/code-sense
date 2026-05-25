import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  FileText,
  Loader2,
  Search,
  Sparkles,
  User,
} from 'lucide-react';

const stageIcons = {
  rephrasing_question: Sparkles,
  selecting_files: Search,
  reading_file: FileText,
  synthesizing_response: Bot,
};

const statusStyles = {
  started: {
    card: 'border-cyan-200 bg-cyan-50/70 text-cyan-900',
    badge: 'border-cyan-200 bg-white text-cyan-700',
    icon: Loader2,
    iconClassName: 'animate-spin',
  },
  completed: {
    card: 'border-emerald-200 bg-emerald-50/70 text-emerald-900',
    badge: 'border-emerald-200 bg-white text-emerald-700',
    icon: CheckCircle2,
    iconClassName: '',
  },
  failed: {
    card: 'border-amber-200 bg-amber-50/80 text-amber-900',
    badge: 'border-amber-200 bg-white text-amber-700',
    icon: AlertTriangle,
    iconClassName: '',
  },
};

function formatLabel(value) {
  if (!value) {
    return 'Progress update';
  }

  return value
    .split('_')
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function getMetadataEntries(metadata) {
  return Object.entries(metadata || {}).filter(([, value]) => {
    if (value === null || value === undefined || value === '') {
      return false;
    }

    return true;
  });
}

export default function ChatMessage({ message, isLatestProgress = false }) {
  if (message.message_type === 'progress_event') {
    const appearance = statusStyles[message.status] || statusStyles.started;
    const StageIcon = stageIcons[message.stage] || appearance.icon;
    const StatusIcon = appearance.icon;
    const metadataEntries = getMetadataEntries(message.metadata);

    return (
      <details
        className={`mb-2 mx-auto max-w-[88%] rounded-xl border px-3 py-2 shadow-sm sm:max-w-[80%] ${appearance.card} fade-in-up`}
        open={isLatestProgress}
      >
        <summary className="flex cursor-pointer list-none items-center gap-2 [&::-webkit-details-marker]:hidden">
          <div className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full border border-current/10 bg-white/70">
            <StageIcon size={13} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <p className="truncate text-xs font-semibold uppercase tracking-wide">{formatLabel(message.stage)}</p>
              <span className={`inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-semibold ${appearance.badge}`}>
                <StatusIcon size={10} className={appearance.iconClassName} />
                {formatLabel(message.status)}
              </span>
            </div>
          </div>
          <div className="text-[10px] text-current/60">
            {new Date(message.created_at).toLocaleTimeString()}
          </div>
        </summary>

        {(message.content || metadataEntries.length > 0) && (
          <div className="ml-8 mt-2 space-y-2">
            {message.content ? (
              <p className="whitespace-pre-wrap text-xs leading-relaxed text-current/85">{message.content}</p>
            ) : null}

            {metadataEntries.length > 0 ? (
              <div className="space-y-1 rounded-lg border border-current/10 bg-white/60 p-2">
                {metadataEntries.map(([key, value]) => (
                  <div key={key} className="text-[11px] text-current/80">
                    <span className="font-semibold">{formatLabel(key)}:</span>{' '}
                    <span className="whitespace-pre-wrap break-words font-mono text-[10px]">
                      {typeof value === 'string' ? value : JSON.stringify(value)}
                    </span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        )}
      </details>
    );
  }

  const isUser = message.role === 'user';

  return (
    <div className={`mb-4 flex gap-3 ${isUser ? 'justify-end' : 'justify-start'} fade-in-up`}>
      {!isUser && (
        <div className="mt-1 flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full border border-cyan-200 bg-cyan-50 text-cyan-700">
          <Bot size={18} />
        </div>
      )}

      <div
        className={`max-w-[80%] rounded-2xl border px-4 py-3 shadow-sm sm:max-w-[72%] ${
          isUser
            ? 'border-cyan-600 bg-cyan-600 text-white'
            : 'border-slate-200 bg-white text-slate-800'
        }`}
      >
        {isUser ? (
          <p className="whitespace-pre-wrap text-sm leading-relaxed">{message.content}</p>
        ) : (
          <div className="prose prose-sm max-w-none prose-headings:text-slate-900 prose-p:text-slate-700 prose-code:text-cyan-700">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
          </div>
        )}
        <div className={`mt-2 text-[11px] ${isUser ? 'text-cyan-100' : 'text-slate-500'}`}>
          {new Date(message.created_at).toLocaleTimeString()}
        </div>
      </div>

      {isUser && (
        <div className="mt-1 flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full border border-slate-300 bg-slate-100 text-slate-600">
          <User size={18} />
        </div>
      )}
    </div>
  );
}
