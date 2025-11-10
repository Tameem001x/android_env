# coding=utf-8
# Copyright 2025 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A class to manage and control an external ADB process."""

import os
import subprocess
import time

from absl import logging
from android_env.components import config_classes
from android_env.components import errors


class AdbController:
  """Manages communication with adb."""

  def __init__(self, config: config_classes.AdbControllerConfig):
    """Instantiates an AdbController object."""

    self._config = config
    logging.info('config: %r', self._config)

    if not self._config.use_adb_server_port_from_os_env:
      # Unset problematic environment variables. ADB commands will fail if these
      # are set. They are normally exported by AndroidStudio.
      if 'ANDROID_HOME' in os.environ:
        logging.info('Removing ANDROID_HOME from os.environ')
        del os.environ['ANDROID_HOME']
      if 'ANDROID_ADB_SERVER_PORT' in os.environ:
        logging.info('Removing ANDROID_ADB_SERVER_PORT from os.environ')
        del os.environ['ANDROID_ADB_SERVER_PORT']

    # Explicitly expand the $HOME environment variable.
    self._os_env_vars = dict(os.environ).copy()
    self._os_env_vars.update(
        {'HOME': os.path.expandvars(self._os_env_vars.get('HOME', ''))}
    )
    logging.info('self._os_env_vars: %r', self._os_env_vars)

  def command_prefix(self, include_device_name: bool = True) -> list[str]:
    """The command for instantiating an adb client to this server."""
    if self._config.use_adb_server_port_from_os_env:
      # When using the adb server port set from the OS environment, we don't
      # need to pass the port explicitly.
      adb_port_args = []
    else:
      # When using the adb server port set from the config, we need to pass the
      # port explicitly.
      adb_port_args = ['-P', str(self._config.adb_server_port)]
    command_prefix = [
        self._config.adb_path,
        *adb_port_args,
    ]
    if include_device_name:
      # Use cloud IP if provided, otherwise use device_name
      cloud_ip = getattr(self._config, 'cloud_ip', None)
      if cloud_ip:
        command_prefix.extend(['-s', f'{cloud_ip}:5555'])
      else:
        command_prefix.extend(['-s', self._config.device_name])
    return command_prefix

  def init_server(self, timeout: float | None = None):
    """Initialize the ADB server deamon on the given port.

    This function should be called immediately after initializing the first
    adb_controller, and before launching the simulator.

    Args:
      timeout: A timeout to use for this operation. If not set the default
        timeout set on the constructor will be used.
    """
    # Make an initial device-independent call to ADB to start the deamon.
    self.execute_command(['devices'], timeout, device_specific=False)
    time.sleep(0.2)

  def _restart_server(self, timeout: float | None = None):
    """Kills and restarts the adb server.

    Args:
      timeout: A timeout to use for this operation. If not set the default
        timeout set on the constructor will be used.
    """
    logging.info('Restarting adb server.')
    self.execute_command(
        ['kill-server'], timeout=timeout, device_specific=False
    )
    time.sleep(0.2)
    cmd_output = self.execute_command(
        ['start-server'], timeout=timeout, device_specific=False
    )
    logging.info('start-server output: %r', cmd_output.decode('utf-8'))
    time.sleep(2.0)
    self.execute_command(['devices'], timeout=timeout, device_specific=False)
    time.sleep(0.2)

  def execute_command(
      self,
      args: list[str],
      timeout: float | None = None,
      device_specific: bool = True,
  ) -> bytes:
    """Executes an adb command.

    Args:
      args: A list of strings representing each adb argument. For example:
        ['install', '/my/app.apk']
      timeout: A timeout to use for this operation. If not set the default
        timeout set on the constructor will be used.
      device_specific: Whether the call is device-specific or independent.

    Returns:
      The output of running such command as a binary string.
    """
    timeout = self._config.default_timeout if timeout is None else timeout
    # Increase timeout for cloud emulators (network latency)
    cloud_ip = getattr(self._config, 'cloud_ip', None)
    if cloud_ip and device_specific and timeout < 60.0:
      timeout = 60.0  # Minimum 60 seconds for cloud emulator commands
    command = self.command_prefix(include_device_name=device_specific) + args
    command_str = 'adb ' + ' '.join(command[1:])

    n_retries = 2
    n_tries = 1
    latest_error = None
    while n_tries <= n_retries:
      try:
        logging.info('Executing ADB command: [%s]', command_str)
        cmd_output = subprocess.check_output(
            command,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=self._os_env_vars,
        )
        logging.debug('ADB command output: %s', cmd_output)
        return cmd_output
      except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logging.exception(
            'Failed to execute ADB command (try %r of 3): [%s]',
            n_tries,
            command_str,
        )
        if e.stdout is not None:
          logging.error('**stdout**:')
          for line in e.stdout.splitlines():
            logging.error('    %s', line)
        if e.stderr is not None:
          logging.error('**stderr**:')
          for line in e.stderr.splitlines():
            logging.error('    %s', line)
        
        # Check if device not found (common with cloud emulators)
        error_output = ''
        if hasattr(e, 'stdout') and e.stdout:
          error_output += e.stdout.decode('utf-8', errors='ignore') if isinstance(e.stdout, bytes) else str(e.stdout)
        if hasattr(e, 'stderr') and e.stderr:
          error_output += e.stderr.decode('utf-8', errors='ignore') if isinstance(e.stderr, bytes) else str(e.stderr)
        if hasattr(e, 'output') and e.output:
          error_output += e.output.decode('utf-8', errors='ignore') if isinstance(e.output, bytes) else str(e.output)
        
        # Handle SELinux errors gracefully (common on cloud emulators)
        error_lower = error_output.lower()
        cloud_ip = getattr(self._config, 'cloud_ip', None)
        if cloud_ip and ('chcon' in error_lower and 'operation not supported on transport endpoint' in error_lower):
          logging.warning('⚠️ SELinux operation not supported on cloud emulator, skipping chcon command...')
          # Return empty successful response (don't raise exception)
          return b''
        
        # Check if it's a timeout that might have actually succeeded
        is_timeout = isinstance(e, subprocess.TimeoutExpired)
        if is_timeout and device_specific:
          # For timeouts, check if we can still communicate with the device
          # Sometimes commands succeed but timeout due to slow response
          cloud_ip = getattr(self._config, 'cloud_ip', None)
          if cloud_ip:
            try:
              # Quick check if device is still connected
              check_cmd = [self._config.adb_path, '-P', str(self._config.adb_server_port), 'devices']
              check_result = subprocess.run(check_cmd, capture_output=True, timeout=5, env=self._os_env_vars, text=True)
              if f'{cloud_ip}:5555' in check_result.stdout and 'device' in check_result.stdout:
                # Device is still connected, might be a false timeout
                # Check if the command might have actually succeeded by looking at stdout
                if hasattr(e, 'stdout') and e.stdout:
                  stdout_str = e.stdout.decode('utf-8', errors='ignore') if isinstance(e.stdout, bytes) else str(e.stdout)
                  # If we see "Complete" or "Status: ok", the command likely succeeded
                  if 'complete' in stdout_str.lower() or 'status: ok' in stdout_str.lower():
                    logging.warning('⚠️ Command timed out but appears to have succeeded, returning partial output')
                    return e.stdout if hasattr(e, 'stdout') and e.stdout else b''
            except Exception:
              pass  # Continue with normal error handling
        
        # Re-check cloud_ip (might have been set above)
        if 'cloud_ip' not in locals():
          cloud_ip = getattr(self._config, 'cloud_ip', None)
        error_lower = error_output.lower()
        if cloud_ip and device_specific and ('not found' in error_lower or 'device offline' in error_lower or 'no devices/emulators found' in error_lower):
          logging.warning('⚠️ Device not found or offline, attempting to reconnect to cloud emulator...')
          try:
            # First, check current devices
            check_cmd = [self._config.adb_path, '-P', str(self._config.adb_server_port), 'devices']
            check_result = subprocess.run(check_cmd, capture_output=True, timeout=5, env=self._os_env_vars, text=True)
            logging.info('Current ADB devices: %s', check_result.stdout)
            
            # Disconnect if already connected (to force fresh connection)
            disconnect_cmd = [self._config.adb_path, '-P', str(self._config.adb_server_port), 'disconnect', f'{cloud_ip}:5555']
            subprocess.run(disconnect_cmd, capture_output=True, timeout=5, env=self._os_env_vars)
            time.sleep(1)
            
            # Reconnect
            reconnect_cmd = [self._config.adb_path, '-P', str(self._config.adb_server_port), 'connect', f'{cloud_ip}:5555']
            reconnect_result = subprocess.run(reconnect_cmd, capture_output=True, timeout=10, env=self._os_env_vars, text=True)
            logging.info('Reconnect output: %s', reconnect_result.stdout)
            time.sleep(3)  # Wait longer for connection to stabilize
            
            # Verify connection
            verify_result = subprocess.run(check_cmd, capture_output=True, timeout=5, env=self._os_env_vars, text=True)
            if f'{cloud_ip}:5555' in verify_result.stdout and 'device' in verify_result.stdout:
              logging.info('✅ Successfully reconnected to cloud emulator')
              # Connection restored, continue with retry
            else:
              logging.warning('⚠️ Reconnection failed, devices: %s', verify_result.stdout)
              # If reconnection failed, skip the command (similar to SELinux errors)
              # This allows environment setup to continue even if some commands fail
              logging.warning('⚠️ Device not found after reconnection attempts, skipping command (similar to SELinux handling)...')
              return b''  # Return empty response to skip the command
          except Exception as reconnect_error:
            logging.warning('Failed to reconnect: %s', reconnect_error)
            # If reconnection failed, skip the command (similar to SELinux errors)
            logging.warning('⚠️ Device not found and reconnection failed, skipping command (similar to SELinux handling)...')
            return b''  # Return empty response to skip the command
        
        n_tries += 1
        latest_error = e
        if device_specific and n_tries <= n_retries:
          self._restart_server(timeout=timeout)

    raise errors.AdbControllerError(
        f'Error executing adb command: [{command_str}]\n'
        f'Caused by: {latest_error}\n'
        f'adb stdout: [{latest_error.stdout}]\n'
        f'adb stderr: [{latest_error.stderr}]'
    ) from latest_error
