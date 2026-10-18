Full reference
==============

Generated from the docstrings. For a quicker overview see :doc:`api`.

Connecting
----------

.. autofunction:: mfatsftp.connect
.. autofunction:: mfatsftp.from_command
.. autofunction:: mfatsftp.build_ssh_argv

Client and files
----------------

.. autoclass:: mfatsftp.SFTPClient
.. autoclass:: mfatsftp.SFTPFile
.. autoclass:: mfatsftp.PipelinedWriter
.. autodata:: mfatsftp.ProgressCallback
   :annotation:

Data types
----------

.. autoclass:: mfatsftp.SFTPAttributes
   :exclude-members: st_size, st_uid, st_gid, st_mode, st_atime, st_mtime, filename, longname, attr
.. autoclass:: mfatsftp.SFTPStatVFS
   :no-members:
.. autoclass:: mfatsftp.AtomicUploadItem
   :no-members:

Exceptions
----------

.. autoexception:: mfatsftp.SFTPError
.. autoexception:: mfatsftp.SFTPNoSuchFileError
.. autoexception:: mfatsftp.SFTPPermissionError
.. autoexception:: mfatsftp.SFTPConnectionError
.. autoexception:: mfatsftp.SFTPTimeoutError

Wire protocol
-------------

.. automodule:: mfatsftp.protocol
   :no-members:
