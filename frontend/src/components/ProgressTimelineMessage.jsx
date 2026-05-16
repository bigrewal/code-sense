import React from 'react';
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  ChevronDown,
  FileText,
  Loader2,
  Search,
  Sparkles,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

const STAGE_ORDER = [
  'rephrasing_question',
  'selecting_files',
  'reading_file',
  'synthesizing_response',
];

const stageIcons = {
  rephrasing_question: Sparkles,
  selecting_files: Search,
  reading_file: FileText,
  synthesizing_response: Bot,
};

function formatLabel(value) {
  if (!value) {
    return 'Unknown';
  }

  return value
    .split('_')
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function formatFileName(filePath) {
  if (!filePath) {
    return 'Unknown file';
  }

  const parts = filePath.split('/');
  return parts[parts.length - 1] || filePath;
}

function getStageAppearance(status) {
  if (status === 'failed') {
    return {
      dot: 'border-amber-300 bg-amber-100 text-amber-700',
      line: 'bg-amber-200/70',
      icon: AlertTriangle,
      iconClassName: '',
      text: 'text-amber-900',
      secondary: 'text-amber-700/80',
    };
  }

  if (status === 'completed') {
    return {
      dot: 'border-emerald-300 bg-emerald-100 text-emerald-700',
      line: 'bg-emerald-200/70',
      icon: CheckCircle2,
      iconClassName: '',
      text: 'text-slate-900',
      secondary: 'text-slate-500',
    };
  }

  if (status === 'started') {
    return {
      dot: 'border-cyan-300 bg-cyan-100 text-cyan-700',
      line: 'bg-slate-200',
      icon: Loader2,
      iconClassName: 'animate-spin',
      text: 'text-slate-900',
      secondary: 'text-slate-500',
    };
  }

  return {
    dot: 'border-slate-200 bg-white text-slate-300',
    line: 'bg-slate-200',
    icon: null,
    iconClassName: '',
    text: 'text-slate-400',
    secondary: 'text-slate-400',
  };
}

function buildStageItems(messages) {
  const byStage = new Map();
  messages.forEach((message) => {
    if (!byStage.has(message.stage)) {
      byStage.set(message.stage, []);
    }

    byStage.get(message.stage).push(message);
  });
  const latestStage = messages[messages.length - 1]?.stage;
  const latestStageIndex = STAGE_ORDER.indexOf(latestStage);
  const selectingFilesMessages = byStage.get('selecting_files') || [];
  const selectingFilesLatest = selectingFilesMessages[selectingFilesMessages.length - 1] || null;
  const noFilesSelected = (
    selectingFilesLatest?.status === 'completed' &&
    selectingFilesLatest?.metadata?.file_count === 0
  );

  return STAGE_ORDER.flatMap((stage, index) => {
    if (stage === 'reading_file' && noFilesSelected) {
      return [];
    }

    const stageMessages = byStage.get(stage) || [];
    const latestMessage = stageMessages[stageMessages.length - 1] || null;
    const status = latestMessage?.status || (latestStageIndex !== -1 && index > latestStageIndex ? 'pending' : null);

    return [{
      stage,
      status,
      message: latestMessage,
      children: stage === 'reading_file' ? stageMessages : [],
      createdAt: latestMessage?.created_at || null,
      note: stage === 'selecting_files' && noFilesSelected ? 'No files selected' : '',
    }];
  });
}

function getChildItems(messages) {
  return (messages || []).map((message) => {
    const filePath = message.metadata?.file_path || '';
    return {
      key: `${filePath || 'file'}-${message.created_at}`,
      label: formatFileName(filePath),
      description: filePath,
      status: message.status,
      infoNeeded: message.metadata?.info_needed || '',
      error: message.metadata?.error || '',
      content: message.content || '',
      createdAt: message.created_at,
    };
  });
}

function getSummary(messages) {
  const latest = messages[messages.length - 1];
  const latestStageLabel = formatLabel(latest?.stage);

  if (latest?.status === 'failed') {
    return {
      title: 'Ran into a problem',
      subtitle: latest?.content || latestStageLabel,
      tone: 'border-amber-200 bg-amber-50/80',
      icon: AlertTriangle,
      iconClassName: '',
    };
  }

  return {
    title: 'Preparing the answer',
    subtitle: latest?.content || latestStageLabel,
    tone: latest?.status === 'completed' ? 'border-emerald-200 bg-emerald-50/70' : 'border-slate-200 bg-slate-50/90',
    icon: latest?.status === 'completed' ? CheckCircle2 : Loader2,
    iconClassName: latest?.status === 'completed' ? '' : 'animate-spin',
  };
}

export default function ProgressTimelineMessage({ messages, message = null }) {
  const stageItems = buildStageItems(messages);
  const summary = getSummary(messages);
  const SummaryIcon = summary.icon;
  const shouldOpenByDefault = !message?.content;

  return (
    <div className="mb-4 flex gap-3 justify-start fade-in-up">
      <div className="mt-1 flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full border border-cyan-200 bg-cyan-50 text-cyan-700">
        <Bot size={18} />
      </div>

      <div className="max-w-[80%] rounded-2xl border border-slate-200 bg-white px-4 py-3 text-slate-800 shadow-sm sm:max-w-[72%]">
        <details className={`group rounded-xl border shadow-sm ${summary.tone}`} open={shouldOpenByDefault}>
          <summary className="flex cursor-pointer list-none items-center gap-3 px-3 py-2.5 [&::-webkit-details-marker]:hidden">
            <div className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full border border-slate-200 bg-white text-slate-700">
              <SummaryIcon size={14} className={summary.iconClassName} />
            </div>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold text-slate-900">{summary.title}</p>
              <p className="truncate text-xs text-slate-500">{summary.subtitle}</p>
            </div>
            <ChevronDown size={16} className="text-slate-400 transition-transform group-open:rotate-180" />
          </summary>

          <div className="border-t border-slate-200/80 px-3 py-3">
            <div className="relative space-y-0">
              {stageItems.map((item, index) => {
                const progressMessage = item.message;
                const appearance = getStageAppearance(item.status);
                const StageIcon = stageIcons[item.stage] || Bot;
                const StatusIcon = appearance.icon;
                const isLast = index === stageItems.length - 1;
                const childItems = getChildItems(item.children);

                return (
                  <div key={item.stage} className="relative pl-10">
                    {!isLast && (
                      <div className={`absolute left-[15px] top-7 h-full w-px ${appearance.line}`} />
                    )}

                    <div className={`absolute left-0 top-0 flex h-8 w-8 items-center justify-center rounded-full border ${appearance.dot}`}>
                      {StatusIcon ? (
                        <StatusIcon size={14} className={appearance.iconClassName} />
                      ) : (
                        <StageIcon size={14} />
                      )}
                    </div>

                    <div className={`pb-5 ${isLast ? 'pb-0' : ''}`}>
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                        <p className={`text-sm font-medium ${appearance.text}`}>{formatLabel(item.stage)}</p>
                        {item.createdAt ? (
                          <span className="text-[11px] text-slate-400">
                            {new Date(item.createdAt).toLocaleTimeString()}
                          </span>
                        ) : null}
                      </div>

                      {item.note ? (
                        <p className="mt-1 text-[11px] text-slate-500">{item.note}</p>
                      ) : null}

                      {childItems.length > 0 ? (
                        <div className="relative mt-3 ml-1 space-y-3 border-l border-slate-200 pl-4">
                          {childItems.map((child) => {
                            const childAppearance = getStageAppearance(child.status);
                            const ChildStatusIcon = childAppearance.icon || FileText;

                            return (
                              <div key={child.key} className="relative">
                                <div className="absolute -left-[21px] top-2 h-px w-3 bg-slate-200" />
                                <div className="flex items-start gap-2">
                                  <div className={`mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full border ${childAppearance.dot}`}>
                                    <ChildStatusIcon size={10} className={childAppearance.iconClassName} />
                                  </div>
                                  <div className="min-w-0">
                                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                                      <p className="text-xs font-medium text-slate-700">{child.label}</p>
                                      {child.createdAt ? (
                                        <span className="text-[10px] text-slate-400">
                                          {new Date(child.createdAt).toLocaleTimeString()}
                                        </span>
                                      ) : null}
                                    </div>
                                    {child.description ? (
                                      <p className="break-words font-mono text-[10px] text-slate-400">{child.description}</p>
                                    ) : null}
                                    {child.error ? (
                                      <p className="mt-1 text-[11px] text-amber-600">{child.error}</p>
                                    ) : null}
                                    {!child.error && child.infoNeeded ? (
                                      <p className="mt-1 text-[11px] text-slate-500">{child.infoNeeded}</p>
                                    ) : null}
                                    {!child.error && !child.infoNeeded && child.content ? (
                                      <p className="mt-1 text-[11px] text-slate-500">{child.content}</p>
                                    ) : null}
                                  </div>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </details>

        {message?.content ? (
          <div className="mt-3 border-t border-slate-100 pt-3">
            <div className="prose prose-sm max-w-none prose-headings:text-slate-900 prose-p:text-slate-700 prose-code:text-cyan-700">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
            </div>
            <div className="mt-2 text-[11px] text-slate-500">
              {new Date(message.created_at).toLocaleTimeString()}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
