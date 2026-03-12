"""Debug integration test."""
import tempfile
import dags
import repl

with tempfile.TemporaryDirectory() as tmpdir:
    dag = repl.create_sample_dag()
    checkpoint_manager = repl.CheckpointManager(tmpdir)
    
    # Initialize git repo
    checkpoint_manager._run_git_command('init')
    checkpoint_manager._run_git_command('config', 'user.email', 'test@example.com')
    checkpoint_manager._run_git_command('config', 'user.name', 'Test User')
    
    repl_instance = repl.MaestroREPL(dag, checkpoint_manager, tmpdir)
    
    # Try to transition task-1 to ACTIVE
    print('Transitioning task-1 to ACTIVE...')
    result = repl_instance._transition_task('task-1', dags.TaskState.ACTIVE, 'Task started')
    print(f'Result: {result}')
    print(f'Task state after ACTIVE: {dag.get_task("task-1").state}')
    
    # Try to transition task-1 to VERIFYING
    print('Transitioning task-1 to VERIFYING...')
    result = repl_instance._transition_task('task-1', dags.TaskState.VERIFYING, 'Task verified')
    print(f'Result: {result}')
    print(f'Task state after VERIFYING: {dag.get_task("task-1").state}')
    
    # Try to transition task-1 to ACCEPTED
    print('Transitioning task-1 to ACCEPTED...')
    result = repl_instance._transition_task('task-1', dags.TaskState.ACCEPTED, 'Task completed')
    print(f'Result: {result}')
    print(f'Task state after ACCEPTED: {dag.get_task("task-1").state}')
