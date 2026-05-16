import React, { useMemo, useState } from 'react';
import {
  X,
  Clock,
  Loader2,
  CheckCircle,
  XCircle,
  Trash2,
  ExternalLink,
} from 'lucide-react';
import JobDetailModal from './JobDetailModal';
import { Api } from '../api/Api';

export default function RepoJobsModal({
  isOpen,
  repoName,
  jobs,
  onClose,
  onRemoveJob,
  onDeleteJob,
}) {
  const [selectedJob, setSelectedJob] = useState(null);
  const [jobDetailOpen, setJobDetailOpen] = useState(false);

  const repoJobs = useMemo(() => {
    if (!repoName) return [];
    return jobs
      .filter((job) => job.repo_name === repoName)
      .sort((a, b) => {
        const aTime = a.updated_at ? new Date(a.updated_at).getTime() : 0;
        const bTime = b.updated_at ? new Date(b.updated_at).getTime() : 0;
        return bTime - aTime;
      });
  }, [jobs, repoName]);

  if (!isOpen) return null;

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

  const getStatusStyles = (status) => {
    switch (status) {
      case 'completed':
        return 'bg-emerald-50 text-emerald-700 border-emerald-200';
      case 'failed':
        return 'bg-red-50 text-red-700 border-red-200';
      case 'cancelled':
        return 'bg-orange-50 text-orange-700 border-orange-200';
      case 'running':
        return 'bg-cyan-50 text-cyan-700 border-cyan-200';
      case 'queued':
      case 'pending':
        return 'bg-amber-50 text-amber-700 border-amber-200';
      default:
        return 'bg-slate-100 text-slate-700 border-slate-200';
    }
  };

  const openJobDetails = async (job) => {
    if (job.isLocalOnly) {
      setSelectedJob(job);
      setJobDetailOpen(true);
      return;
    }

    try {
      const fullJobDetails = await Api.getJobStatus(job.jobId);
      setSelectedJob(fullJobDetails);
      setJobDetailOpen(true);
    } catch (err) {
      console.error('Failed to fetch job details:', err);
      setSelectedJob(job);
      setJobDetailOpen(true);
    }
  };

  return (
    <>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/35 p-4">
        <div className="w-full max-w-3xl rounded-2xl border border-slate-200 bg-white shadow-xl">
          <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4">
            <div>
              <h3 className="text-lg font-semibold text-slate-900">Repository Jobs</h3>
              <p className="text-sm text-slate-500">{repoName}</p>
            </div>
            <button onClick={onClose} className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600">
              <X size={20} />
            </button>
          </div>

          <div className="max-h-[70vh] overflow-y-auto p-4">
            {repoJobs.length === 0 ? (
              <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 px-4 py-8 text-center text-sm text-slate-500">
                No ingestion jobs found for this repository.
              </div>
            ) : (
              <div className="space-y-2">
                {repoJobs.map((job) => (
                  <div key={job.jobId} className="rounded-xl border border-slate-200 bg-white p-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          {getStatusIcon(job.status)}
                          <span className={`rounded-full border px-2 py-0.5 text-xs font-semibold ${getStatusStyles(job.status)}`}>
                            {job.status}
                          </span>
                        </div>
                        <p className="mt-2 text-xs text-slate-500">Job ID: {job.job_id || job.jobId}</p>
                        {job.current_stage && (
                          <p className="mt-1 text-xs text-slate-600 capitalize">
                            Stage: {job.current_stage.replace('_', ' ')}
                          </p>
                        )}
                        {job.error && (
                          <p className="mt-1 truncate text-xs text-red-600">{job.error}</p>
                        )}
                      </div>

                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => openJobDetails(job)}
                          className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100"
                        >
                          Details
                          <ExternalLink size={13} />
                        </button>

                        {job.isLocalOnly ? (
                          <button
                            onClick={() => onRemoveJob(job.jobId)}
                            className="rounded p-1.5 text-slate-500 hover:bg-slate-100"
                            title="Remove item"
                          >
                            <X size={14} />
                          </button>
                        ) : (
                          ['completed', 'failed', 'cancelled'].includes(job.status) && (
                            <button
                              onClick={() => onDeleteJob(job.jobId)}
                              className="rounded p-1.5 text-red-600 hover:bg-red-50"
                              title="Delete job"
                            >
                              <Trash2 size={14} />
                            </button>
                          )
                        )}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      <JobDetailModal
        job={selectedJob}
        isOpen={jobDetailOpen}
        onClose={() => {
          setJobDetailOpen(false);
          setSelectedJob(null);
        }}
      />
    </>
  );
}
