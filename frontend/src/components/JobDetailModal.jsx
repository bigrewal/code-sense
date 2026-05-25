import React from 'react';
import { X, CheckCircle, XCircle, Clock, Loader2 } from 'lucide-react';

export default function JobDetailModal({ job, isOpen, onClose }) {
  if (!isOpen || !job) return null;

  const getStatusIcon = (status) => {
    switch (status) {
      case 'completed':
        return <CheckCircle size={22} className="text-emerald-600" />;
      case 'failed':
        return <XCircle size={22} className="text-red-600" />;
      case 'cancelled':
        return <XCircle size={22} className="text-orange-600" />;
      case 'running':
        return <Loader2 size={22} className="animate-spin text-cyan-600" />;
      case 'queued':
      case 'pending':
        return <Clock size={22} className="text-amber-600" />;
      default:
        return <Clock size={22} className="text-slate-600" />;
    }
  };

  const getStatusColor = (status) => {
    switch (status) {
      case 'completed':
        return 'text-emerald-700 bg-emerald-50';
      case 'failed':
        return 'text-red-700 bg-red-50';
      case 'cancelled':
        return 'text-orange-700 bg-orange-50';
      case 'running':
        return 'text-cyan-700 bg-cyan-50';
      case 'queued':
      case 'pending':
        return 'text-amber-700 bg-amber-50';
      default:
        return 'text-slate-700 bg-slate-50';
    }
  };

  const formatDate = (timestamp) => {
    if (!timestamp) return 'N/A';
    const date = typeof timestamp === 'number'
      ? new Date(timestamp * 1000)
      : new Date(timestamp);
    return date.toLocaleString();
  };

  const stages = job.stages || {};
  const stageOrder = ['precheck', 'mental_model'];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/45 p-4 backdrop-blur-sm">
      <div className="surface-card max-h-[82vh] w-full max-w-3xl overflow-y-auto rounded-2xl p-6">
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-slate-900">Job Details</h3>
          <button onClick={onClose} className="rounded p-1 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600">
            <X size={20} />
          </button>
        </div>

        <div className="mb-5 rounded-xl border border-slate-200 bg-white p-4">
          <div className="mb-3 flex items-center gap-3">
            {getStatusIcon(job.status)}
            <div className="flex-1">
              <h4 className="font-semibold text-slate-900">{job.repo_name}</h4>
              <p className="text-sm text-slate-500">Job ID: {job.job_id || job.jobId}</p>
            </div>
            <span className={`rounded-full px-3 py-1 text-sm font-semibold capitalize ${getStatusColor(job.status)}`}>
              {job.status}
            </span>
          </div>

          <div className="grid grid-cols-1 gap-3 text-sm text-slate-700 sm:grid-cols-2">
            <div><span className="text-slate-500">Created:</span> <span className="font-medium">{formatDate(job.created_at)}</span></div>
            <div><span className="text-slate-500">Updated:</span> <span className="font-medium">{formatDate(job.updated_at)}</span></div>
            {job.current_stage && (
              <div className="sm:col-span-2">
                <span className="text-slate-500">Current Stage:</span>
                <span className="ml-2 font-medium capitalize">{job.current_stage.replace('_', ' ')}</span>
              </div>
            )}
          </div>
        </div>

        {job.error && (
          <div className="mb-5 rounded-xl border border-red-200 bg-red-50 p-4">
            <h5 className="mb-1 font-semibold text-red-900">Error</h5>
            <p className="text-sm text-red-700">{job.error}</p>
          </div>
        )}

        <div>
          <h5 className="mb-3 text-base font-semibold text-slate-900">Pipeline Stages</h5>
          <div className="space-y-3">
            {stageOrder.map(stageName => {
              const stage = stages[stageName] || {};
              const stageStatus = stage.status || 'pending';

              return (
                <div key={stageName} className="rounded-xl border border-slate-200 bg-white p-3">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="font-medium capitalize text-slate-900">
                      {stageName.replace('_', ' ')}
                    </span>
                    <span className={`rounded px-2 py-1 text-xs font-semibold capitalize ${getStatusColor(stageStatus)}`}>
                      {stageStatus}
                    </span>
                  </div>

                  {stageName === 'precheck' && stage.metrics && (
                    <div className="space-y-1 border-l-2 border-slate-200 pl-2 text-xs text-slate-600">
                      <div>Supported Tokens: {stage.metrics.supported_tokens?.toLocaleString()}</div>
                      <div>Supported File count: {stage.metrics.supported_file_count}</div>
                      {stage.metrics.language_distribution_pct &&
                        Object.keys(stage.metrics.language_distribution_pct).length > 0 && (
                          <div className="mt-2 border-t border-slate-200 pt-2">
                            <div className="mb-1 font-medium">Language distribution:</div>
                            <div className="space-y-0.5 pl-1">
                              {Object.entries(stage.metrics.language_distribution_pct).map(([language, percentage]) => (
                                <div key={language}>
                                  {language.charAt(0).toUpperCase() + language.slice(1)}: {percentage}%
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                    </div>
                  )}

                  {stageName === 'mental_model' && stage.metrics && (
                    <div className="space-y-1 border-l-2 border-slate-200 pl-2 text-xs text-slate-600">
                      <div>Critical Files: {stage.metrics.critical_files}</div>
                      <div>Files Ignored: {stage.metrics.files_ignored}</div>
                      <div>Context Tokens: {stage.metrics.repo_context_token_count?.toLocaleString()}</div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
