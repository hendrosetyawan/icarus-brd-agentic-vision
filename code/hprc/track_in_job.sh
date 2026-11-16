#!/bin/bash
# Run all 6 tracking tasks at once on the GPU of an already-running job (e.g. the A100 agent job while it
# waits for tracks), instead of queueing hprc/track.slurm:
#   srun --jobid=<JOBID> --overlap -c 16 bash hprc/track_in_job.sh
source hprc/env.sh
for i in 0 1 2 3 4 5; do
  OMP_NUM_THREADS=2 DECODE_THREADS=3 python -m brd.track --task "$i" > "logs/track_in_job_$i.log" 2>&1 &
done
wait
echo "all tracking tasks finished: $(date +%H:%M)"
