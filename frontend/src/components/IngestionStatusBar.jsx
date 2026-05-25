import React, { useState } from 'react';
import { Loader2, CheckCircle, XCircle, Clock, Trash2, X } from 'lucide-react';
import JobDetailModal from './JobDetailModal';
import { Api } from '../api/Api';

export default function IngestionStatusBar({ jobs, onRemoveJob, onDeleteJob }) {
  const [selectedJob, setSelectedJob] = useState(null);
  const [detailModalOpen, setDetailModalOpen] = useState(false);

  if (jobs.length === 0) return null;

  const handleJobClick = async (job) => {
    if (job.isLocalOnly) {
      setSelectedJob(job);
      setDetailModalOpen(true);
      return;
    }

    try {
      const fullJobDetails = await Api.getJobStatus(job.jobId);
      setSelectedJob(fullJobDetails);
      setDetailModalOpen(true);
    } catch (err) {
      console.error('Failed to fetch job details:', err);
      setSelectedJob(job);
      setDetailModalOpen(true);
    }
  };

  const getStatusIcon = (status) => {
    switch (status) {
      case 'completed':
        return <CheckCircle size={15} className="text-emerald-600" />;
      case 'failed':
        return <XCircle size={15} className="text-red-600" />;
      case 'cancelled':
        return <XCircle size={15} className="text-orange-600" />;
      case 'running':
        return <Loader2 size={15} className="animate-spin text-cyan-600" />;
      case 'queued':
      case 'pending':
        return <Clock size={15} className="text-amber-600" />;
      default:
        return <Clock size={15} className="text-slate-600" />;
    }
  };

  const getStatusColor = (status) => {
    switch (status) {
      case 'completed':
        return 'bg-emerald-50 border-emerald-200';
      case 'failed':
        return 'bg-red-50 border-red-200';
      case 'cancelled':
        return 'bg-orange-50 border-orange-200';
      case 'running':
        return 'bg-cyan-50 border-cyan-200';
      case 'queued':
      case 'pending':
        return 'bg-amber-50 border-amber-200';
      default:
        return 'bg-slate-50 border-slate-200';
    }
  };

  const canDeletePersistedJob = (status) => {
    return status === 'completed' || status === 'failed' || status === 'cancelled';
  };

  const sortedJobs = [...jobs].sort((a, b) => {
    const aTime = a.updated_at ? new Date(a.updated_at).getTime() : 0;
    const bTime = b.updated_at ? new Date(b.updated_at).getTime() : 0;
    return bTime - aTime;
  });

  return (
    <div className="mt-4 border-t border-slate-200 pt-4">
      <div className="mb-2 px-1 text-sm font-semibold text-slate-700">Ingestion Jobs</div>

      <div className="space-y-2">
        {sortedJobs.map((job) => (
          <div
            key={job.jobId}
            onClick={() => handleJobClick(job)}
            className={`cursor-pointer rounded-xl border p-2 transition hover:opacity-90 ${getStatusColor(job.status)}`}
          >
            <div className="mb-1 flex items-center justify-between">
              <div className="flex min-w-0 flex-1 items-center gap-2">
                {getStatusIcon(job.status)}
                <span className="truncate text-xs font-semibold text-slate-800">
                  {job.repo_name?.split('/').pop() || 'Unknown'}
                </span>
              </div>
              <div className="flex items-center gap-1">
                {job.isLocalOnly ? (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onRemoveJob(job.jobId);
                    }}
                    className="rounded p-1 text-slate-500 transition hover:bg-slate-200"
                    title="Remove item"
                  >
                    <X size={13} />
                  </button>
                ) : (
                  canDeletePersistedJob(job.status) && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        onDeleteJob(job.jobId);
                      }}
                      className="rounded p-1 text-red-600 transition hover:bg-red-100"
                      title="Delete job"
                    >
                      <Trash2 size={13} />
                    </button>
                  )
                )}
              </div>
            </div>

            <div className="text-xs text-slate-600">
              {job.current_stage && job.status === 'running' && (
                <div className="capitalize">Stage: {job.current_stage.replace('_', ' ')}</div>
              )}
              {job.error && (
                <div className="mt-1 truncate text-red-600">{job.error}</div>
              )}
            </div>
          </div>
        ))}
      </div>

      <JobDetailModal
        job={selectedJob}
        isOpen={detailModalOpen}
        onClose={() => {
          setDetailModalOpen(false);
          setSelectedJob(null);
        }}
      />
    </div>
  );
}
