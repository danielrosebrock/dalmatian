import pandas as pd
import numpy as np
import subprocess
import os
import io
from collections import defaultdict
import firecloud.api
from firecloud import fiss
import iso8601
import pytz
from datetime import datetime

#------------------------------------------------------------------------------
#  Extension of firecloud.api functionality using the rawls (internal) API
#------------------------------------------------------------------------------
def _batch_update_entities(namespace, workspace, json_body):
    """ Batch update entity attributes in a workspace.

    Args:
        namespace (str): project to which workspace belongs
        workspace (str): Workspace name
        json_body (list(dict)):
        [{
            "name": "string",
            "entityType": "string",
            "operations": (list(dict))
        }]

        operations:
        [{
          "op": "AddUpdateAttribute",
          "attributeName": "string",
          "addUpdateAttribute": "string"
        },
        {
          "op": "RemoveAttribute",
          "attributeName": "string"
        },
        {
          "op": "AddListMember",
          "attributeListName": "string",
          "newMember": "string"
        },
        {
          "op": "RemoveListMember",
          "attributeListName": "string",
          "removeMember": "string"
        },
        {
          "op": "CreateAttributeEntityReferenceList",
          "attributeListName": "string"
        },
        {
          "op": "CreateAttributeValueList",
          "attributeListName": "string"
        }]

    Swagger:
        https://rawls.dsde-prod.broadinstitute.org/#!/entities/batch_update_entities
    """
    headers = firecloud.api._fiss_agent_header({"Content-type":  "application/json"})
    uri = "{0}workspaces/{1}/{2}/entities/batchUpdate".format(
        'https://rawls.dsde-prod.broadinstitute.org/api/', namespace, workspace)

    return firecloud.api.__post(uri, headers=headers, json=json_body)


#------------------------------------------------------------------------------
#  Top-level classes representing workspace(s)
#------------------------------------------------------------------------------
class WorkspaceCollection(object):
    def __init__(self):
        self.workspace_list = []

    def add(self, workspace_manager):
        assert isinstance(workspace_manager, WorkspaceManager)
        self.workspace_list.append(workspace_manager)

    def remove(self, workspace_manager):
        self.workspace_list.remove(workspace_manager)

    def print_workspaces(self):
        print('Workspaces in collection:')
        for i in self.workspace_list:
            print('  {}/{}'.format(i.namespace, i.workspace))

    def get_submission_status(self, show_namespaces=False):
        """Get status of all submissions across workspaces"""
        dfs = []
        for i in self.workspace_list:
            df = i.get_submission_status(show_namespaces=show_namespaces)
            if show_namespaces:
                df['workspace'] = '{}/{}'.format(i.namespace, i.workspace)
            else:
                df['workspace'] = i.workspace
            dfs.append(df)
        return pd.concat(dfs, axis=0)


class WorkspaceManager(object):
    def __init__(self, namespace, workspace, timezone='America/New_York'):
        self.namespace = namespace
        self.workspace = workspace
        self.timezone  = timezone


    def create_workspace(self, wm=None):
        """Create the workspace, or clone from another"""
        if wm is None:
            r = firecloud.api.create_workspace(self.namespace, self.workspace)
            if r.status_code==201:
                print('Workspace {}/{} successfully created.'.format(self.namespace, self.workspace))
            elif r.status_code==409:
                print(r.json()['message'])
            else:
                print(r.text)
        else:  # clone workspace
            r = firecloud.api.clone_workspace(wm.namespace, wm.workspace, self.namespace, self.workspace)
            if r.status_code==201:
                print('Workspace {}/{} successfully cloned from {}/{}.'.format(
                    self.namespace, self.workspace, wm.namespace, wm.workspace))
            else:
                print(r.text)


    def delete_workspace(self):
        """Delete the workspace"""
        r = firecloud.api.delete_workspace(self.namespace, self.workspace)
        if r.status_code==202:
            print('Workspace {}/{} successfully deleted.'.format(self.namespace, self.workspace))
            print('  * '+r.json()['message'])
        else:
            print(r.text)


    def get_bucket_id(self):
        """Get the GCS bucket ID associated with the workspace"""
        r = firecloud.api.get_workspace(self.namespace, self.workspace)
        assert r.status_code==200
        r = r.json()
        bucket_id = r['workspace']['bucketName']
        return bucket_id


    def upload_samples(self, df, participant_df=None, add_participant_samples=False):
        """
        Upload samples stored in a pandas DataFrame, and populate the required
        participant, sample, and sample_set attributes

        df columns: sample_id (index), participant_id, {sample_set_id,} other attributes
        """
        assert df.index.name=='sample_id' and df.columns[0]=='participant_id'

        # 1) upload participant IDs (without additional attributes)
        if participant_df is None:
            participant_ids = np.unique(df['participant_id'])
            participant_df = pd.DataFrame(data=participant_ids, columns=['entity:participant_id'])
        else:
            assert (participant_df.index.name=='entity:participant_id'
                or participant_df.columns[0]=='entity:participant_id')

        buf = io.StringIO()
        participant_df.to_csv(buf, sep='\t', index=participant_df.index.name=='entity:participant_id')
        s = firecloud.api.upload_entities_tsv(self.namespace, self.workspace, buf)
        buf.close()
        if s.status_code==200:
            print('Successfully imported {} participants.'.format(participant_df.shape[0]))
        else:
            print(s.text)
            raise ValueError('Participant import failed.')

        # 2) upload samples
        sample_df = df[df.columns[df.columns!='sample_set_id']].copy()
        sample_df.index.name = 'entity:sample_id'
        buf = io.StringIO()
        sample_df.to_csv(buf, sep='\t')
        s = firecloud.api.upload_entities_tsv(self.namespace, self.workspace, buf)
        buf.close()
        if s.status_code==200:
            print('Successfully imported {} samples.'.format(sample_df.shape[0]))
        else:
            print(s.text)
            raise ValueError('Sample import failed.')

        # 3 upload sample sets
        if 'sample_set_id' in df.columns:
            set_df = pd.DataFrame(data=sample_df.index.values, columns=['sample_id'])
            set_df.index = df['sample_set_id']
            set_df.index.name = 'membership:sample_set_id'
            buf = io.StringIO()
            set_df.to_csv(buf, sep='\t')
            s = firecloud.api.upload_entities_tsv(self.namespace, self.workspace, buf)
            buf.close()
            assert s.status_code==200
            print('Successfully imported {} sample sets.'.format(len(df['sample_set_id'].unique())))

        if add_participant_samples:
            # 4) add participant.samples_
            print('  * The FireCloud data model currently does not provide participant.samples\n',
                  '    Adding "participant.samples_" as an explicit attribute.', sep='')
            self.update_participant_samples()


    def upload_participants(self, participant_ids):
        """Upload a list of participants IDs"""
        participant_df = pd.DataFrame(data=np.unique(participant_ids), columns=['entity:participant_id'])
        buf = io.StringIO()
        participant_df.to_csv(buf, sep='\t', index=participant_df.index.name=='entity:participant_id')
        s = firecloud.api.upload_entities_tsv(self.namespace, self.workspace, buf)
        buf.close()
        assert s.status_code==200
        print('Successfully imported participants.')


    def update_participant_samples(self):
        """Attach samples to participants"""
        df = self.get_samples()[['participant']]
        samples_dict = {k:g.index.values for k,g in df.groupby('participant')}

        participant_ids = np.unique(df['participant'])
        for j,k in enumerate(participant_ids):
            print('\r    Updating samples for participant {}/{}'.format(j+1,len(participant_ids)), end='')
            attr_dict = {
                "samples_": {
                    "itemsType": "EntityReference",
                    "items": [{"entityType": "sample", "entityName": i} for i in samples_dict[k]]
                }
            }
            attrs = [firecloud.api._attr_set(i,j) for i,j in attr_dict.items()]
            r = firecloud.api.update_entity(self.namespace, self.workspace, 'participant', k, attrs)
            assert r.status_code==200
        print('\n    Finished updating participants in {}/{}'.format(self.namespace, self.workspace))


    def update_participant_samples_and_pairs(self):
        """Attach samples and pairs to participants"""
        df = self.get_samples()[['participant']]
        samples_dict = {k:g.index.values for k,g in df.groupby('participant')}

        participant_ids = np.unique(df['participant'])
        for j,k in enumerate(participant_ids):
            print('\r    Updating samples for participant {}/{}'.format(j+1,len(participant_ids)), end='')
            attr_dict = {
                "samples_": {
                    "itemsType": "EntityReference",
                    "items": [{"entityType": "sample", "entityName": i} for i in samples_dict[k]]
                }
            }
            attrs = [firecloud.api._attr_set(i,j) for i,j in attr_dict.items()]
            r = firecloud.api.update_entity(self.namespace, self.workspace, 'participant', k, attrs)
            assert r.status_code==200
        print('\n    Finished attaching samples to participants in {}/{}'.format(self.namespace, self.workspace))

        df = self.get_pairs()[['participant']]
        pairs_dict = {k: g.index.values for k, g in df.groupby('participant')}

        participant_ids = np.unique(df['participant'])
        for j, k in enumerate(participant_ids):
            print('\r    Updating pairs for participant {}/{}'.format(j + 1, len(participant_ids)), end='')
            attr_dict = {
                "pairs_": {
                    "itemsType": "EntityReference",
                    "items": [{"entityType": "pair", "entityName": i} for i in pairs_dict[k]]
                }
            }
            attrs = [firecloud.api._attr_set(i, j) for i, j in attr_dict.items()]
            r = firecloud.api.update_entity(self.namespace, self.workspace, 'participant', k, attrs)
            assert r.status_code == 200
        print('\n    Finished attaching pairs to participants in {}/{}'.format(self.namespace, self.workspace))


    def make_pairs(self, sample_set_id=None):
        """
        Make all possible pairs from participants (all or a specified set)
        Requires sample_type sample level annotation 'Normal' or 'Tumor'
        """
        # get data from sample set or all samples
        if sample_set_id == None:
            df = self.get_samples()
        else:
            df = self.get_sample_attributes_in_set(sample_set_id)

        normal_samples = list(df[df['sample_type'] == 'Normal'].index)
        participants = list(df['participant'])
        # generate pairs
        pair_tumors = list()
        pair_normals = list()
        pair_ids = list()
        participant_pair_ids = list()
        for s in normal_samples:
            patient = df['participant'][df.index == s][0]
            idx = [i for i, x in enumerate(participants) if x == patient]
            patient_sample_tsv = df.iloc[idx]
            for i, row in patient_sample_tsv.iterrows():
                if not row['sample_type'] == 'Normal':
                    pair_tumors.append(i)
                    pair_normals.append(s)
                    pair_ids.append(i + '-' + s)
                    participant_pair_ids.append(patient)
        columns = ['entity:pair_id', 'case_sample', 'control_sample', 'participant']
        pair_df = pd.DataFrame(index=pair_ids, columns=columns)
        pair_df['entity:pair_id'] = pair_ids
        pair_df['case_sample'] = pair_tumors
        pair_df['control_sample'] = pair_normals
        pair_df['participant'] = participant_pair_ids
        buf = io.StringIO()
        pair_df.to_csv(buf, sep='\t', index=False)
        s = firecloud.api.upload_entities_tsv(self.namespace, self.workspace, buf)
        buf.close()
        if s.status_code == 200:
            print('Successfully imported {} pairs'.format(pair_df.shape[0]))
        else:
            print(s.text)
            raise ValueError('Pair import failed.')


    def update_sample_attributes(self, sample_id, attrs):
        """Set or update attributes in attrs (pd.Series or pd.DataFrame)"""
        self.update_entity_attributes('sample', attrs)


    def update_sample_set_attributes(self, sample_set_id, attrs):
        """
        Set or update attributes in attrs (pd.Series or pd.DataFrame)
        """
        self.update_entity_attributes('sample_set', attrs)


    def delete_sample_set_attributes(self, sample_set_id, attrs):
        """Delete attributes"""
        self.delete_entity_attributes(self, 'sample_set', sample_set_id, attrs)


    def update_attributes(self, attr_dict):
        """
        Set or update workspace attributes. Wrapper for API 'set' call
        """
        attrs = [firecloud.api._attr_set(i,j) for i,j in attr_dict.items()]
        r = firecloud.api.update_workspace_attributes(self.namespace, self.workspace, attrs)  # attrs must be list
        assert r.status_code==200
        print('Successfully updated workspace attributes in {}/{}'.format(self.namespace, self.workspace))


    def get_attributes(self):
        """Get workspace attributes"""
        r = firecloud.api.get_workspace(self.namespace, self.workspace)
        assert r.status_code==200
        attr = r.json()['workspace']['attributes']
        for k in [k for k in attr if 'library:' in k]:
            attr.pop(k)
        return attr


    def get_sample_attributes_in_set(self, set):
        """Get sample attributes of samples in a set"""
        samples = self.get_sample_sets().loc[set]['samples']
        all_samples = self.get_samples().index
        idx = np.zeros(len(all_samples), dtype=bool)
        for s in samples:
            idx[all_samples == s] = True
        return self.get_samples()[idx]


    def get_submission_status(self, filter_active=False, config=None, show_namespaces=False):
        """
        Get status of all submissions in the workspace (replicates UI Monitor)
        """
        # filter submissions by configuration
        submissions = self.list_submissions(config=config)

        statuses = ['Succeeded', 'Running', 'Failed', 'Aborted', 'Submitted', 'Queued']
        df = []
        for s in submissions:
            d = {
                'entity_id':s['submissionEntity']['entityName'],
                'status':s['status'],
                'submission_id':s['submissionId'],
                'date':iso8601.parse_date(s['submissionDate']).strftime('%H:%M:%S %m/%d/%Y'),
            }
            d.update({i:s['workflowStatuses'].get(i,0) for i in statuses})
            if show_namespaces:
                d['configuration'] = s['methodConfigurationNamespace']+'/'+s['methodConfigurationName']
            else:
                d['configuration'] = s['methodConfigurationName']
            df.append(d)
        df = pd.DataFrame(df)
        df.set_index('entity_id', inplace=True)
        df['date'] = pd.to_datetime(df['date'])
        df = df[['configuration', 'status']+statuses+['date', 'submission_id']]
        if filter_active:
            df = df[(df['Running']!=0) | (df['Submitted']!=0)]
        return df.sort_values('date')[::-1]


    def get_workflow_metadata(self, submission_id, workflow_id):
        """Get metadata JSON for a specific workflow"""
        metadata = firecloud.api.get_workflow_metadata(self.namespace, self.workspace,
            submission_id, workflow_id)
        assert metadata.status_code==200
        return metadata.json()


    def get_submission(self, submission_id):
        """Get submission metadata"""
        r = firecloud.api.get_submission(self.namespace, self.workspace, submission_id)
        assert r.status_code==200
        return r.json()


    def list_submissions(self, config=None):
        """List all submissions from workspace"""
        submissions = firecloud.api.list_submissions(self.namespace, self.workspace)
        assert submissions.status_code==200
        submissions = submissions.json()

        if config is not None:
            submissions = [s for s in submissions if config in s['methodConfigurationName']]

        return submissions


    def list_configs(self):
        """List configurations in workspace"""
        r = firecloud.api.list_workspace_configs(self.namespace, self.workspace)
        assert r.status_code==200
        return r.json()


    def print_scatter_status(self, submission_id, workflow_id=None):
        """Print status for a specific scatter job"""
        if workflow_id is None:
            s = self.get_submission(submission_id)
            assert len(s['workflows'])==1
            workflow_id = s['workflows'][0]['workflowId']
        metadata = self.get_workflow_metadata(submission_id, workflow_id)
        for task_name in metadata['calls']:
            if np.all(['shardIndex' in i for i in metadata['calls'][task_name]]):
                print('Submission status ({}): {}'.format(task_name.split('.')[-1], metadata['status']))
                s = pd.Series([s['backendStatus'] for s in metadata['calls'][task_name]])
                print(s.value_counts().to_string())


    def get_entity_status(self, etype, config):
        """Get status of latest submission for the entity type in the workspace"""

        # filter submissions by configuration
        submissions = self.list_submissions(config=config)

        # get status of last run submission
        entity_dict = {}
        for k,s in enumerate(submissions):
            print('\rFetching submission {}/{}'.format(k+1, len(submissions)), end='')
            if s['submissionEntity']['entityType']!=etype:
                print('\rIncompatible submission entity type: {}'.format(
                    s['submissionEntity']['entityType']))
                print('\rSkipping : '+ s['submissionId'])
                continue
            r = self.get_submission(s['submissionId'])
            ts = datetime.timestamp(iso8601.parse_date(s['submissionDate']))
            for w in r['workflows']:
                entity_id = w['workflowEntity']['entityName']
                if entity_id not in entity_dict or entity_dict[entity_id]['timestamp']<ts:
                    entity_dict[entity_id] = {
                        'status':w['status'],
                        'timestamp':ts,
                        'submission_id':s['submissionId'],
                        'configuration':s['methodConfigurationName']
                    }
                    if 'workflowId' in w:
                        entity_dict[entity_id]['workflow_id'] = w['workflowId']
                    else:
                        entity_dict[entity_id]['workflow_id'] = 'NA'
        print()
        status_df = pd.DataFrame(entity_dict).T
        status_df.index.name = etype+'_id'

        return status_df[['status', 'timestamp', 'workflow_id', 'submission_id', 'configuration']]


    def get_sample_status(self, configuration):
        """Get status of lastest submission for samples in the workspace"""
        return self.get_entity_status('sample', configuration)

    def get_pair_status(self, configuration):
        """Get status of lastest submission for samples in the workspace"""
        return self.get_entity_status('pair', configuration)

    def get_pair_set_status(self, configuration):
        """Get status of lastest submission for samples in the workspace"""
        return self.get_entity_status('pair_set', configuration)

    def get_sample_set_status(self, configuration):
        """Get status of lastest submission for sample sets in the workspace"""
        return self.get_entity_status('sample_set', configuration)


    def patch_attributes(self, cnamespace, configuration, dry_run=False, entity='sample'):
        """
        Patch attributes for all samples/tasks that run successfully but were not written to database.
        This includes outputs from successful tasks in workflows that failed.
        """

        # get list of expected outputs
        r = firecloud.api.get_workspace_config(self.namespace, self.workspace, cnamespace, configuration)
        assert r.status_code==200
        r = r.json()
        output_map = {i.split('.')[-1]:j.split('this.')[-1] for i,j in r['outputs'].items()}
        columns = list(output_map.values())

        if entity=='sample':
            # get list of all samples in workspace
            print('Fetching sample status ...')
            samples_df = self.get_samples()
            if len(np.intersect1d(columns, samples_df.columns))>0:
                incomplete_df = samples_df[samples_df[columns].isnull().any(axis=1)]
            else:
                incomplete_df = pd.DataFrame(index=samples_df.index, columns=columns)

            # get workflow status for all submissions
            sample_status_df = self.get_sample_status(configuration)

            # make sure successful workflows were all written to database
            error_ix = incomplete_df.loc[sample_status_df.loc[incomplete_df.index, 'status']=='Succeeded'].index
            if np.any(error_ix):
                print('Attributes from {} successful jobs were not written to database.'.format(len(error_ix)))

            # for remainder, assume that if attributes exists, status is successful.
            # this doesn't work when multiple successful runs of the same task exist --> need to add this

            # for incomplete samples, go through submissions and assign outputs of completed tasks
            task_counts = defaultdict(int)
            for n,sample_id in enumerate(incomplete_df.index):
                print('\rPatching attributes for sample {}/{}'.format(n+1, incomplete_df.shape[0]), end='')

                try:
                    metadata = self.get_workflow_metadata(sample_status_df.loc[sample_id, 'submission_id'], sample_status_df.loc[sample_id, 'workflow_id'])
                    if 'outputs' in metadata and len(metadata['outputs'])!=0 and not dry_run:
                        attr = {output_map[k.split('.')[-1]]:t for k,t in metadata['outputs'].items()}
                        self.update_sample_attributes(sample_id, attr)
                    else:
                        for task in metadata['calls']:
                            if 'outputs' in metadata['calls'][task][-1]:
                                if np.all([k in output_map for k in metadata['calls'][task][-1]['outputs'].keys()]):
                                    # only update if attributes are empty
                                    if incomplete_df.loc[sample_id, [output_map[k] for k in metadata['calls'][task][-1]['outputs']]].isnull().any():
                                        # write to attributes
                                        if not dry_run:
                                            attr = {output_map[i]:j for i,j in metadata['calls'][task][-1]['outputs'].items()}
                                            self.update_sample_attributes(sample_id, attr)
                                        task_counts[task.split('.')[-1]] += 1
                except:
                    print('Metadata call failed for sample {}'.format(sample_id))
                    print(metadata.json())
            print()
            for i,j in task_counts.items():
                print('Samples patched for "{}": {}'.format(i,j))

        elif entity=='sample_set':
            print('Fetching sample set status ...')
            sample_set_df = self.get_sample_sets()
            # get workflow status for all submissions
            sample_set_status_df = self.get_sample_set_status(configuration)

            # any sample sets with empty attributes for configuration
            incomplete_df = sample_set_df.loc[sample_set_status_df.index, columns]
            incomplete_df = incomplete_df[incomplete_df.isnull().any(axis=1)]

            # sample sets with successful jobs
            error_ix = incomplete_df[sample_set_status_df.loc[incomplete_df.index, 'status']=='Succeeded'].index
            if np.any(error_ix):
                print('Attributes from {} successful jobs were not written to database.'.format(len(error_ix)))
                print('Patching attributes with outputs from latest successful run.')
                for n,sample_set_id in enumerate(incomplete_df.index):
                    print('\r  * Patching sample set {}/{}'.format(n+1, incomplete_df.shape[0]), end='')
                    metadata = self.get_workflow_metadata(sample_set_status_df.loc[sample_set_id, 'submission_id'], sample_set_status_df.loc[sample_set_id, 'workflow_id'])
                    if 'outputs' in metadata and len(metadata['outputs'])!=0 and not dry_run:
                        attr = {output_map[k.split('.')[-1]]:t for k,t in metadata['outputs'].items()}
                        self.update_sample_set_attributes(sample_set_id, attr)
                print()
        print('Completed patching {} attributes in {}/{}'.format(entity, self.namespace, self.workspace))


    def display_status(self, configuration, entity='sample', filter_active=True):
        """
        Display summary of task statuses
        """
        # workflow status for each sample (from latest/current run)
        status_df = self.get_sample_status(configuration)

        # get workflow details from 1st submission
        metadata = self.get_workflow_metadata(status_df['submission_id'][0], status_df['workflow_id'][0])

        workflow_tasks = list(metadata['calls'].keys())

        print(status_df['status'].value_counts())
        if filter_active:
            ix = status_df[status_df['status']!='Succeeded'].index
        else:
            ix = status_df.index

        state_df = pd.DataFrame(0, index=ix, columns=workflow_tasks)
        for k,i in enumerate(ix):
            print('\rFetching metadata for sample {}/{}'.format(k+1, len(ix)), end='')
            metadata = self.get_workflow_metadata(status_df.loc[i, 'submission_id'], status_df.loc[i, 'workflow_id'])
            state_df.loc[i] = [metadata['calls'][t][-1]['executionStatus'] if t in metadata['calls'] else 'Waiting' for t in workflow_tasks]
        print()
        state_df.rename(columns={i:i.split('.')[1] for i in state_df.columns}, inplace=True)
        summary_df = pd.concat([state_df[c].value_counts() for c in state_df], axis=1).fillna(0).astype(int)
        print(summary_df)
        state_df[['workflow_id', 'submission_id']] = status_df.loc[ix, ['workflow_id', 'submission_id']]

        return state_df, summary_df


    def get_stderr(self, state_df, task_name):
        """
        Fetch stderrs from bucket (returns list of str)
        """
        df = state_df[state_df[task_name]==-1]
        fail_idx = df.index
        stderrs = []
        for n,i in enumerate(fail_idx):
            print('\rFetching stderr for task {}/{}'.format(n+1, len(fail_idx)), end='\r')
            metadata = self.get_workflow_metadata(state_df.loc[i, 'submission_id'], state_df.loc[i, 'workflow_id'])
            stderr_path = metadata['calls'][[i for i in metadata['calls'].keys() if i.split('.')[1]==task_name][0]][-1]['stderr']
            s = subprocess.check_output('gsutil cat '+stderr_path, shell=True).decode()
            stderrs.append(s)
        return stderrs


    def get_submission_history(self, sample_id, config=None):
        """
        Currently only supports samples
        """

        # filter submissions by configuration
        submissions = self.list_submissions(config=config)

        # filter by sample
        submissions = [s for s in submissions if s['submissionEntity']['entityName']==sample_id and 'Succeeded' in list(s['workflowStatuses'].keys())]

        outputs_df = []
        for s in submissions:
            r = self.get_submission(s['submissionId'])

            metadata = self.get_workflow_metadata(s['submissionId'], r['workflows'][0]['workflowId'])

            outputs_s = pd.Series(metadata['outputs'])
            outputs_s.index = [i.split('.',1)[1].replace('.','_') for i in outputs_s.index]
            outputs_s['submission_date'] = iso8601.parse_date(s['submissionDate']).strftime('%H:%M:%S %m/%d/%Y')
            outputs_df.append(outputs_s)

        outputs_df = pd.concat(outputs_df, axis=1).T
        # sort by most recent first
        outputs_df = outputs_df.iloc[np.argsort([datetime.timestamp(iso8601.parse_date(s['submissionDate'])) for s in submissions])[::-1]]
        outputs_df.index = ['run_{}'.format(str(i)) for i in np.arange(outputs_df.shape[0],0,-1)]

        return outputs_df


    def get_storage(self):
        """
        Get total amount of storage used, in TB

        Pricing: $0.026/GB/month (multi-regional)
                 $0.02/GB/month (regional)
        """
        bucket_id = self.get_bucket_id()
        s = subprocess.check_output('gsutil du -s gs://'+bucket_id, shell=True)
        return np.float64(s.decode().split()[0])/1024**4


    def get_stats(self, status_df, workflow_name=None):
        """
        For a list of submissions, calculate time, preemptions, etc
        """
        # for successful jobs, get metadata and count attempts
        status_df = status_df[status_df['status']=='Succeeded'].copy()
        metadata_dict = {}
        for k,(i,row) in enumerate(status_df.iterrows()):
            print('\rFetching metadata {}/{}'.format(k+1,status_df.shape[0]), end='')
            fetch = True
            while fetch:  # improperly dealing with 500s here...
                try:
                    metadata = self.get_workflow_metadata(row['submission_id'], row['workflow_id'])
                    metadata_dict[i] = metadata.json()
                    fetch = False
                except:
                    pass

        # if workflow_name is None:
            # split output by workflow
        workflows = np.array([metadata_dict[k]['workflowName'] for k in metadata_dict])
        # else:
            # workflows = np.array([workflow_name])

        # get tasks for each workflow
        for w in np.unique(workflows):
            workflow_status_df = status_df[workflows==w]
            tasks = np.sort(list(metadata_dict[workflow_status_df.index[0]]['calls'].keys()))
            task_names = [t.rsplit('.')[-1] for t in tasks]

            task_dfs = {}
            for t in tasks:
                task_name = t.rsplit('.')[-1]
                task_dfs[task_name] = pd.DataFrame(index=workflow_status_df.index,
                    columns=[
                        'time_h',
                        'total_time_h',
                        'max_preempt_time_h',
                        'machine_type',
                        'attempts',
                        'start_time',
                        'est_cost',
                        'job_ids'])
                for i in workflow_status_df.index:
                    successes = {}
                    preemptions = []

                    if 'shardIndex' in metadata_dict[i]['calls'][t][0]:
                        scatter = True
                        for j in metadata_dict[i]['calls'][t]:
                            if j['shardIndex'] in successes:
                                preemptions.append(j)
                            # last shard (assume success follows preemptions)
                            successes[j['shardIndex']] = j
                    else:
                        scatter = False
                        successes[0] = metadata_dict[i]['calls'][t][-1]
                        preemptions = metadata_dict[i]['calls'][t][:-1]

                    task_dfs[task_name].loc[i, 'time_h'] = np.sum([workflow_time(j)/3600 for j in successes.values()])

                    # subtract time spent waiting for quota
                    quota_time = [e for m in successes.values() for e in m['executionEvents'] if e['description']=='waiting for quota']
                    quota_time = [(convert_time(q['endTime']) - convert_time(q['startTime']))/3600 for q in quota_time]
                    task_dfs[task_name].loc[i, 'time_h'] -= np.sum(quota_time)

                    total_time_h = [workflow_time(t_attempt)/3600 for t_attempt in metadata_dict[i]['calls'][t]]
                    task_dfs[task_name].loc[i, 'total_time_h'] = np.sum(total_time_h) - np.sum(quota_time)

                    if not np.any(['hit' in j['callCaching'] and j['callCaching']['hit'] for j in metadata_dict[i]['calls'][t]]):
                        was_preemptible = [j['preemptible'] for j in metadata_dict[i]['calls'][t]]
                        if len(preemptions)>0:
                            assert was_preemptible[0]
                            task_dfs[task_name].loc[i, 'max_preempt_time_h'] = np.max([workflow_time(t_attempt) for t_attempt in preemptions])/3600
                        task_dfs[task_name].loc[i, 'attempts'] = len(metadata_dict[i]['calls'][t])

                        task_dfs[task_name].loc[i, 'start_time'] = iso8601.parse_date(metadata_dict[i]['calls'][t][0]['start']).astimezone(pytz.timezone(self.timezone)).strftime('%H:%M')

                        machine_types = [j['jes']['machineType'].rsplit('/')[-1] for j in metadata_dict[i]['calls'][t]]
                        task_dfs[task_name].loc[i, 'machine_type'] = machine_types[-1]  # use last instance

                        task_dfs[task_name].loc[i, 'est_cost'] = np.sum([get_vm_cost(m,p)*h for h,m,p in zip(total_time_h, machine_types, was_preemptible)])

                        task_dfs[task_name].loc[i, 'job_ids'] = ','.join([j['jobId'] for j in successes.values()])

            # add overall cost
            workflow_status_df['est_cost'] = pd.concat([task_dfs[t.rsplit('.')[-1]]['est_cost'] for t in tasks], axis=1).sum(axis=1)
            workflow_status_df['time_h'] = [workflow_time(metadata_dict[i])/3600 for i in workflow_status_df.index]
            workflow_status_df['cpu_hours'] = pd.concat([task_dfs[t.rsplit('.')[-1]]['total_time_h'] * task_dfs[t.rsplit('.')[-1]]['machine_type'].apply(lambda i: int(i.rsplit('-',1)[-1]) if (pd.notnull(i) and '-small' not in i and '-micro' not in i) else 1) for t in tasks], axis=1).sum(axis=1)
            workflow_status_df['start_time'] = [iso8601.parse_date(metadata_dict[i]['start']).astimezone(pytz.timezone(self.timezone)).strftime('%H:%M') for i in workflow_status_df.index]

        return workflow_status_df, task_dfs


    def publish_config(self, from_cnamespace, from_config, to_cnamespace, to_config, public=False):
        """Copy configuration to repository"""
        # check whether prior version exists
        r = get_config(to_cnamespace, to_config)
        old_version = None
        if r:
            old_version = np.max([m['snapshotId'] for m in r])
            print('Configuration {}/{} exists. SnapshotID: {}'.format(
                to_cnamespace, to_config, old_version))

        # copy config to repo
        r = firecloud.api.copy_config_to_repo(self.namespace, self.workspace, from_cnamespace, from_config, to_cnamespace, to_config)
        assert r.status_code==200
        print("Successfully copied {}/{}. New SnapshotID: {}".format(to_cnamespace, to_config, r.json()['snapshotId']))

        # make configuration public
        if public:
            print('  * setting public read access.')
            r = firecloud.api.update_repository_config_acl(to_cnamespace, to_config, r.json()['snapshotId'], [{'role': 'READER', 'user': 'public'}])

        # delete old version
        if old_version is not None:
            r = firecloud.api.delete_repository_config(to_cnamespace, to_config, old_version)
            assert r.status_code==200
            print("Successfully deleted SnapshotID {}.".format(old_version))


    def import_config(self, cnamespace, cname):
        """Import configuration from repository"""
        # get latest snapshot
        c = get_config(cnamespace, cname)
        if len(c)==0:
            raise ValueError('Configuration "{}/{}" not found (name must match exactly).'.format(cnamespace, cname))
        c = c[np.argmax([i['snapshotId'] for i in c])]
        r = firecloud.api.copy_config_from_repo(self.namespace, self.workspace, cnamespace, cname, c['snapshotId'], cnamespace, cname)
        if r.status_code==201:
            print('Successfully imported configuration "{}/{}" (SnapshotId {})'.format(cnamespace, cname, c['snapshotId']))
        else:
            print(r.text)


    #-------------------------------------------------------------------------
    #  Methods for querying entities
    #-------------------------------------------------------------------------
    def _get_entities_query(self, etype, page, page_size=1000):
        """
        Wrapper for firecloud.api.get_entities_query
        """
        r = firecloud.api.get_entities_query(self.namespace, self.workspace,
                etype, page=page, page_size=page_size)
        assert r.status_code==200
        return r.json()


    def get_entities(self, etype, page_size=1000):
        """Paginated query replacing get_entities_tsv()"""
        # get first page
        r = self._get_entities_query(etype, 1, page_size=page_size)

        # get additional pages
        total_pages = r['resultMetadata']['filteredPageCount']
        all_entities = r['results']
        for page in range(2,total_pages+1):
            r = self._get_entities_query(etype, page, page_size=page_size)
            all_entities.extend(r['results'])

        # convert to DataFrame
        df = pd.DataFrame({i['name']:i['attributes'] for i in all_entities}).T
        df.index.name = etype+'_id'
        return df


    def get_samples(self):
        """Get DataFrame with samples and their attributes"""
        df = self.get_entities('sample')
        df['participant'] = df['participant'].apply(lambda x: x['entityName'])
        return df


    def get_pairs(self):
        """Get DataFrame with pairs and their attributes"""
        df = self.get_entities('pair')
        df['participant'] = df['participant'].apply(lambda x: x['entityName'])
        df['case_sample'] = df['case_sample'].apply(lambda  x: x['entityName'])
        df['control_sample'] = df['control_sample'].apply(lambda x: x['entityName'])
        return df


    def get_participants(self):
        """Get DataFrame with participants and their attributes"""
        df = self.get_entities('participant')
        return df


    def get_sample_sets(self):
        """Get DataFrame with sample sets and their attributes"""
        r = firecloud.api.get_entities(self.namespace, self.workspace, 'sample_set')
        assert r.status_code==200
        r = r.json()

        # convert JSON to table
        sample_set_ids = [i['name'] for i in r]
        columns = np.unique([k for s in r for k in s['attributes'].keys()])
        df = pd.DataFrame(index=sample_set_ids, columns=columns)
        for s in r:
            for c in columns:
                if c in s['attributes']:
                    if isinstance(s['attributes'][c], dict):
                        df.loc[s['name'], c] = [i['entityName'] if 'entityName' in i else i for i in s['attributes'][c]['items']]
                    else:
                        df.loc[s['name'], c] = s['attributes'][c]
        return df


    #-------------------------------------------------------------------------
    #  Methods for updating entities
    #-------------------------------------------------------------------------
    def update_sample_set(self, sample_set_id, sample_ids):
        """Update (or create) a sample set"""
        r = firecloud.api.get_entity(self.namespace, self.workspace, 'sample_set', sample_set_id)
        if r.status_code==200:  # exists -> update
            r = r.json()
            items_dict = r['attributes']['samples']
            items_dict['items'] = [{'entityName': i, 'entityType': 'sample'} for i in sample_ids]
            attrs = [{'addUpdateAttribute': items_dict, 'attributeName': 'samples', 'op': 'AddUpdateAttribute'}]
            r = firecloud.api.update_entity(self.namespace, self.workspace, 'sample_set', sample_set_id, attrs)
            if r.status_code==200:
                print('Sample set "{}" ({} samples) successfully updated.'.format(sample_set_id, len(sample_ids)))
            else:
                print(r.text)
        else:  # create
            set_df = pd.DataFrame(data=np.c_[[sample_set_id]*len(sample_ids), sample_ids], columns=['membership:sample_set_id', 'sample_id'])
            buf = io.StringIO()
            set_df.to_csv(buf, sep='\t', index=False)
            r = firecloud.api.upload_entities(self.namespace, self.workspace, buf.getvalue())
            buf.close()
            assert r.status_code==200
            print('Sample set "{}" ({} samples) successfully created.'.format(sample_set_id, len(sample_ids)))


    def update_pair_set(self, pair_set_id, pair_ids):
        """Update (or create) a pair set"""
        r = firecloud.api.get_entity(self.namespace, self.workspace, 'pair_set_id', pair_set_id)
        if r.status_code==200:  # exists -> update
            r = r.json()
            items_dict = r['attributes']['pairs']
            items_dict['items'] = [{'entityName': i, 'entityType': 'pair'} for i in pair_ids]
            attrs = [{'addUpdateAttribute': items_dict, 'attributeName': 'pairs', 'op': 'AddUpdateAttribute'}]
            r = firecloud.api.update_entity(self.namespace, self.workspace, 'pair_set', pair_set_id, attrs)
            if r.status_code==200:
                print('Pair set "{}" ({} pairs) successfully updated.'.format(pair_set_id, len(pair_ids)))
            else:
                print(r.text)
        else:  # create
            set_df = pd.DataFrame(data=np.c_[[pair_set_id]*len(pair_ids), pair_ids], columns=['membership:pair_set_id', 'pair_id'])
            buf = io.StringIO()
            set_df.to_csv(buf, sep='\t', index=False)
            r = firecloud.api.upload_entities(self.namespace, self.workspace, buf.getvalue())
            buf.close()
            assert r.status_code==200
            print('Pair set "{}" ({} pairs) successfully created.'.format(pair_set_id, len(pair_ids)))


    def update_participant_set(self, participant_set_id, participant_ids):
        """Update (or create) a participant set"""
        r = firecloud.api.get_entity(self.namespace, self.workspace, 'participant_set', participant_set_id)
        if r.status_code==200:  # exists -> update
            raise ValueError('not implemented')
        else:  # create
            set_df = pd.DataFrame(data=np.c_[[participant_set_id]*len(participant_ids), participant_ids], columns=['membership:participant_set_id', 'participant_id'])
            buf = io.StringIO()
            set_df.to_csv(buf, sep='\t', index=False)
            r = firecloud.api.upload_entities(self.namespace, self.workspace, buf.getvalue())
            buf.close()
            assert r.status_code==200
            print('Participant set "{}" ({} participants) successfully created.'.format(participant_set_id, len(participant_ids)))


    def update_super_set(self, super_set_id, sample_set_ids, sample_ids):
        """
        Update (or create) a set of sample sets

        Defines the attribute "sample_sets_"

        sample_ids: at least one 'dummy' sample is needed
        """
        if isinstance(sample_ids, str):
            self.update_sample_set(super_set_id, [sample_ids])
        else:
            self.update_sample_set(super_set_id, sample_ids)

        attr_dict = {
            "sample_sets_": {
                "itemsType": "EntityReference",
                "items": [{"entityType": "sample_set", "entityName": i} for i in sample_set_ids]
            }
        }
        attrs = [firecloud.api._attr_set(i,j) for i,j in attr_dict.items()]
        r = firecloud.api.update_entity(self.namespace, self.workspace, 'sample_set', super_set_id, attrs)
        if r.status_code==200:
            print('Set of sample sets "{}" successfully created.'.format(super_set_id))
        else:
            print(r.text)


    #-------------------------------------------------------------------------
    #  Methods for deleting entities
    #-------------------------------------------------------------------------
    def delete_entity_attributes(self, delete_s, etype, delete_files=False):
        """
        Delete sample attributes and their associated data

        delete_s: pd.Series with sample_id -> path; delete_s.name is attribute to delete
        """
        op = [{'attributeName':delete_s.name, 'op':'RemoveAttribute'}]
        attrs = [{'name':i, 'entityType':etype, 'operations': op} for i in delete_s.index]
        r = _batch_update_entities(self.namespace, self.workspace, attrs)
        if r.status_code==204:
            print("Successfully deleted attribute '{}' for {} samples.".format(delete_s.name, len(delete_s)))

            if delete_files:
                print('Deleting files')
                gs_delete(delete_s)
        else:
            print(r.text)

    # def delete_entity_attributes(self, etype, ename, attrs, check=True):
    #     """
    #     Delete attributes
    #     """
    #     if isinstance(attrs, str):
    #         rm_list = [{"op": "RemoveAttribute", "attributeName": attrs}]
    #     elif isinstance(attrs, Iterable):
    #         rm_list = [{"op": "RemoveAttribute", "attributeName": i} for i in attrs]
    #     r = firecloud.api.update_entity(self.namespace, self.workspace, etype, ename, rm_list)
    #     if check:
    #         assert r.status_code==200
    #     else:
    #         return r

    def delete_sample(self, sample_ids):
        """Delete sample or list of samples"""
        r = firecloud.api.delete_sample(self.namespace, self.workspace, sample_ids)
        assert r.status_code==204
        # IF LAST SAMPLE, ALSO NEED TO DELETE PARTICIPANT -- no longer seems to be the case?


    def delete_participant(self, participant_ids):
        """
        Delete participant or list of participants
        """
        r = firecloud.api.delete_participant(self.namespace, self.workspace, participant_ids)
        assert r.status_code==204


    def delete_sample_set(self, sample_set_id):
        """Delete sample set"""
        r = firecloud.api.delete_sample_set(self.namespace, self.workspace, sample_set_id)
        assert r.status_code==204
        print('Sample set "{}" successfully deleted.'.format(sample_set_id))


    def delete_pair_set(self, pair_set_id):
        """Delete pair set"""
        r = firecloud.api.delete_pair_set(self.namespace, self.workspace, pair_set_id)
        assert r.status_code==204
        print('Pair set "{}" successfully deleted.'.format(pair_set_id))


    #-------------------------------------------------------------------------
    #  
    #-------------------------------------------------------------------------
    def find_sample_set(self, sample_id, sample_set_df=None):
        """Find sample set(s) containing sample"""
        if sample_set_df is None:
            sample_set_df = self.get_sample_sets()
        return sample_set_df[sample_set_df['samples'].apply(lambda x: sample_id in x)].index.tolist()


    def purge_outdated(self, attribute, bucket_files=None, samples_df=None, ext=None):
        """
        Delete outdated files matching attribute (e.g., from prior/outdated runs)
        """
        if bucket_files is None:
            bucket_files = gs_list_bucket_files(self.get_bucket_id())

        if samples_df is None:
            samples_df = self.get_samples()

        try:
            assert attribute in samples_df.columns
        except:
            raise ValueError('Sample attribute "{}" does not exist'.format(attribute))

        # make sure all samples have attribute set
        assert samples_df[attribute].isnull().sum()==0

        if ext is None:
            ext = np.unique([os.path.split(i)[1].split('.',1)[1] for i in samples_df[attribute]])
            assert len(ext)==1
            ext = ext[0]

        purge_paths = [i for i in bucket_files if i.endswith(ext) and i not in set(samples_df[attribute])]
        if len(purge_paths)==0:
            print('No outdated files to purge.')
        else:
            bucket_id = self.get_bucket_id()
            assert np.all([i.startswith('gs://'+bucket_id) for i in purge_paths])

            while True:
                s = input('{} outdated files found. Delete? [y/n] '.format(len(purge_paths))).lower()
                if s=='n' or s=='y':
                    break

            if s=='y':
                print('Purging {} outdated files.'.format(len(purge_paths)))
                gs_delete(purge_paths, chunk_size=500)


    def update_entity_attributes(self, etype, attrs):
        """
        Create or update entity attributes

        attrs:
          pd.DataFrame: update entities x attributes
          pd.Series:    update attribute (attr.name)
                        for multiple entities (attr.index)

          To update multiple attributes for a single entity, use:
            pd.DataFrame(attr_dict, index=[entity_name]))

          To update a single attribute for a single entity, use:
            pd.Series({attr_name:attr_value}, name=entity_name)
        """
        if isinstance(attrs, pd.DataFrame):
            attr_list = []
            for i,row in attrs.iterrows():
                attr_list.extend([{
                    'name':row.name,
                    'entityType':etype,
                    'operations': [{"op": "AddUpdateAttribute", "attributeName": i, "addUpdateAttribute":str(j)} for i,j in row.iteritems()]
                }])
        elif isinstance(attrs, pd.Series):
            attr_list = [{
                'name':i,
                'entityType':etype,
                'operations': [{"op": "AddUpdateAttribute", "attributeName":attrs.name, "addUpdateAttribute":str(j)}]
            } for i,j in attrs.iteritems()]
        else:
            raise ValueError('Unsupported input format.')

        # try rawls batch call if available
        r = _batch_update_entities(self.namespace, self.workspace, attr_list)
        # try:  # TODO
        if r.status_code==204:
            if isinstance(attrs, pd.DataFrame):
                print("Successfully updated attributes '{}' for {} {}s.".format(attrs.columns.tolist(), attrs.shape[0], etype))
            elif isinstance(attrs, pd.Series):
                print("Successfully updated attribute '{}' for {} {}s.".format(attrs.name, len(attrs), etype))
            else:
                print("Successfully updated attribute '{}' for {} {}s.".format(attrs.name, len(attrs), etype))
        else:
            print(r.text)

        # # revert to public API:
        # def update_entity_attributes(self, etype, ename, attr_dict):
        #     """
        #     Set or update attributes in attr_dict
        #     """
        #     attrs = [firecloud.api._attr_set(i,j) for i,j in attr_dict.items()]
        #     r = firecloud.api.update_entity(self.namespace, self.workspace, etype, ename, attrs)
        #     if r.status_code==200:
        #         print('Successfully updated {}.'.format(ename))
        #     else:
        #         print(r.text)
        #


    def update_configuration(self, json_body):
        """
        Create or update a method configuration (separate API calls)

        json_body = {
           'namespace': config_namespace,
           'name': config_name,
           'rootEntityType' : entity,
           'methodRepoMethod': {'methodName':method_name, 'methodNamespace':method_namespace, 'methodVersion':version},
           'methodNamespace': method_namespace,
           'inputs':  {},
           'outputs': {},
           'prerequisites': {},
           'deleted': False
        }

        """
        configs = self.list_configs()
        if json_body['name'] not in [m['name'] for m in configs]:
            # configuration doesn't exist -> name, namespace specified in json_body
            r = firecloud.api.create_workspace_config(self.namespace, self.workspace, json_body)
            if r.status_code==201:
                print('Successfully added configuration: {}'.format(json_body['name']))
            else:
                print(r.text)
        else:
            r = firecloud.api.update_workspace_config(self.namespace, self.workspace, json_body['namespace'], json_body['name'], json_body)
            if r.status_code==200:
                print('Successfully updated configuration: {}'.format(json_body['name']))
            else:
                print(r.text)


    def check_configuration(self, config_name):
        """
        Get version of a configuration and compare to latest available in repository
        """
        r = self.list_configs()
        r = [i for i in r if i['name']==config_name][0]
        # method repo version
        mrversion = get_method_version(r['methodRepoMethod']['methodNamespace'], r['methodRepoMethod']['methodName'])
        print('Method for config. {0}: {1} version {2} (latest: {3})'.format(config_name, r['methodRepoMethod']['methodName'], r['methodRepoMethod']['methodVersion'], mrversion))
        return r['methodRepoMethod']['methodVersion']


    def get_configs(self, latest_only=False):
        """
        Get all configurations in the workspace
        """
        r = self.list_configs()
        df = pd.io.json.json_normalize(r)
        df.rename(columns={c:c.split('methodRepoMethod.')[-1] for c in df.columns}, inplace=True)
        if latest_only:
            df = df.sort_values(['methodName','methodVersion'], ascending=False).groupby('methodName').head(1)
            # .sort_values('methodName')
            # reverse sort
            return df[::-1]
        return df


    def create_submission(self, cnamespace, config, entity, etype, expression=None, use_callcache=True):
        """

        """
        r = firecloud.api.create_submission(self.namespace, self.workspace,
            cnamespace, config, entity, etype, expression=expression, use_callcache=use_callcache)
        if r.status_code==201:
            print('Successfully created submission {}.'.format(r.json()['submissionId']))
        else:
            print(r.text)
